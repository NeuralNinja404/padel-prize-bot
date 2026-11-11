#!/usr/bin/env python3
"""PADO prize digest: upcoming Kyiv padel tournaments whose prize is worth at least N UAH.

Tournaments come from PADO's API (api.pado.app). Prize amounts are read from each
tournament's title and description; racket prizes are priced at Ukrainian padel shops
(padel-tennis.com.ua, tennisgo.com.ua) and the prices are cached for a week.

This module is the core of the Telegram bot (bot.py / app.py, deployed on Vercel) and also a
command-line tool for checking the list locally. Standard library only (Python 3.9+):

    python3 pado_prizes.py           # print the report and save it to out/
    python3 pado_prizes.py --quiet   # only save it

Local runs keep their caches in data/ and write out/prizes.md, out/prizes.html and out/prizes.json.
Settings: config.json.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "out")
LOG_DIR = os.path.join(ROOT, "logs")

API = "https://api.pado.app"
SITE = "https://pado.app"
# PADO's own app link for a tournament: on a phone with the PADO app installed it opens the app (iOS claims
# every pado.app/d/* link, and Android verifies pado.app links); anywhere else it redirects to the web page.
TOURNAMENT_LINK = SITE + "/d/tournament/{id}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
KYIV = ZoneInfo("Europe/Kyiv")

DEFAULT_CONFIG = {
    "city": "Kyiv",                    # as PADO's API spells it
    "city_name": "Київ",               # how texts name the city
    "city_in": "у Києві",              # "in Kyiv", for sentences
    "country": "Ukraine",
    "min_prize_uah": 5000,
    "days_ahead": 90,
    "include_full": True,              # keep full tournaments (the waitlist may still work)
    "show_maybe": True,                # also list prizes that can't be valued yet
    "price_cache_days": 7,
}

log = logging.getLogger("pado_prizes")


# --------------------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(os.path.join(ROOT, "config.json"), {}))
    return cfg


class FileStore:
    """Small JSON documents ("state", "prices", "clubs", "latest", ...) as files in a local folder.

    The bot uses BlobStore (blob_store.py) with the same get/put/update interface on Vercel."""

    def __init__(self, folder):
        self.folder = folder

    def _path(self, key):
        return os.path.join(self.folder, key + ".json")

    def get(self, key, default=None, fresh=True):  # files are always fresh; `fresh` matters for BlobStore
        return load_json(self._path(key), default)

    def put(self, key, value):
        save_json(self._path(key), value)

    def update(self, key, change, default=None):
        """Read a document, apply change(document) -> new document, write it back if it changed, return it."""
        current = self.get(key, copy.deepcopy(default))
        before = copy.deepcopy(current)
        value = change(current)
        if value != before:
            self.put(key, value)
        return value


def http(url, params=None, accept="text/html,application/json;q=0.9", timeout=30, retries=2):
    """GET with retries and a 1 s pause after every call."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": accept, "Accept-Language": "uk,en;q=0.8"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
            time.sleep(1.0)  # stay polite: about one request per second
            return text
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries:
                raise
        except (urllib.error.URLError, OSError):
            if attempt == retries:
                raise
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def fmt_uah(value):
    return f"{int(value):,}".replace(",", " ") + " ₴"


WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня",
          "листопада", "грудня"]


def fmt_day(d):
    """Сб, 14 листопада"""
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def city_name(cfg):
    return cfg.get("city_name") or cfg["city"]


def tidy(text, limit=90):
    """One line, no leading emoji/bullets, cut to `limit` characters."""
    text = re.sub(r"[￼﻿​-‏⁠]", "", text)  # object-replacement / zero-width junk
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[^\w]+", "", text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def display_name(name):
    return tidy(re.sub(r"\s*\|\s*", " · ", name.strip()), 110)


# --------------------------------------------------------------------------- PADO

def fetch_tournaments(cfg, now):
    params = {
        "city": cfg["city"],
        "country": cfg["country"],
        "dateFrom": now.date().isoformat(),
        "dateTo": (now.date() + dt.timedelta(days=int(cfg["days_ahead"]))).isoformat(),
        "is_show_filled_out": "true",
        "limit": 1000,
    }
    data = json.loads(http(API + "/v2/tournament", params, accept="application/json"))
    if isinstance(data, dict):
        data = data.get("data") or data.get("items") or []
    if len(data) >= params["limit"]:
        log.warning("PADO returned %d tournaments, the request limit; some may be missing", len(data))
    return data


def start_time(t):
    tz = ZoneInfo(t.get("timezone") or "Europe/Kyiv")
    day = dt.datetime.fromisoformat(t["startDate"].replace("Z", "+00:00")).astimezone(tz).date()
    hh, mm = ((t.get("timeFrom") or "00:00") + ":00")[:5].split(":")
    return dt.datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=tz)


def club_names(tournaments, store):
    """Tournament id -> club name. Uses the tournament's location text, else PADO's club endpoints (cached)."""
    cache = store.get("clubs", {}) or {}

    def key(t):
        if t.get("clubId"):
            return f"club:{t['clubId']}"
        if t.get("intClubId"):
            return f"int:{t['intClubId']}"
        return ""

    for t in tournaments:  # learn names from tournaments that carry one
        k, loc = key(t), (t.get("customLocation") or "").strip()
        if k and loc and k not in cache:
            cache[k] = loc
    names = {}
    for t in tournaments:
        k, loc = key(t), (t.get("customLocation") or "").strip()
        if not loc and k and k not in cache:
            kind, cid = k.split(":")
            try:
                d = json.loads(http(f"{API}/{'clubs' if kind == 'club' else 'internal-clubs'}/{cid}",
                                    accept="application/json"))
                d = d.get("data", d) if isinstance(d, dict) else {}
                cache[k] = (d.get("name") or d.get("clubName") or "").strip()
            except Exception as e:  # a missing club name isn't worth failing the run
                log.warning("club lookup %s failed: %s", k, e)
        names[t["id"]] = loc or cache.get(k, "") or ((t.get("community") or {}).get("name") or "").strip()
    store.put("clubs", cache)
    return names


def who_can_play(name):
    n = name.lower()
    women = re.search(r"girls?\b|ladies|women|жіноч|дівчат", n)
    men = re.search(r"\bmen\b|\bmen[’']s|чоловіч|хлопц", n)
    if women and not men:
        return "лише для жінок"
    if men and not women:
        return "лише для чоловіків"
    return ""


# --------------------------------------------------------------------------- prize text

BRANDS = [
    ("Adidas", r"adidas"), ("Bullpadel", r"bull\s?padel"), ("NOX", r"\bnox\b"), ("Babolat", r"babolat"),
    ("Head", r"\bhead\b"), ("Wilson", r"\bwilson\b"), ("Siux", r"\bsiux\b"), ("StarVie", r"star\s?vie"),
    ("Drop Shot", r"drop\s?shot"), ("Varlion", r"\bvarlion\b"), ("Black Crown", r"black\s?crown"),
    ("Vibora", r"\bv[ií]bora\b"), ("Kombat", r"\bkombat\b"), ("Dunlop", r"\bdunlop\b"), ("Oxdog", r"\boxdog\b"),
    ("Royal Padel", r"royal\s?padel"), ("Kuikma", r"\bkuikma\b"), ("Joma", r"\bjoma\b"), ("Enebe", r"\benebe\b"),
    ("Tecnifibre", r"tecnifibre"), ("Puma", r"\bpuma\b"), ("Asics", r"\basics\b"), ("Akkeron", r"\bakkeron\b"),
    ("Hirostar", r"\bhirostar\b"), ("Dabber", r"\bdabber\b"), ("Vairo", r"\bvairo\b"), ("Orygen", r"\borygen\b"),
    ("Mystica", r"\bmystica\b"), ("Yonex", r"\byonex\b"), ("Prince", r"\bprince\b"), ("Lok", r"\blok\b"),
]
BRAND_RES = [(name, re.compile(pat, re.I)) for name, pat in BRANDS]
BRAND_WORDS = {w for name, _ in BRANDS for w in name.lower().split()} | {"bull"}

RACKET_WORD = re.compile(r"ракет|\bracket\b(?!\s+sports)|raqueta", re.I)
PRIZE_WORDS = re.compile(r"приз|місц|переможц|виграй|виграти|граємо на|розіграш|головн|нагород|"
                         r"winner|prize|🥇|🥈|🥉|🏆|🎁", re.I)
MONEY_WORDS = re.compile(r"сертиф|номінал|приз|грошов|виграш|винагород|нагород|подарунков|voucher|"
                         r"certificate|gift card|prize|cash|фонд|переможц|місц|🥇|🥈|🥉|🏆", re.I)
FEE_WORDS = re.compile(r"вартіст|ціна|внесок|оплат|знижк|депозит|участ|вхід|\bcost|\bprice|\bfee\b|entry", re.I)
NOT_A_RACKET_PRIZE = re.compile(r"намотк|овергрип|overgrip|\bгрип|м['’ʼ`]?яч|\bballs?\b|сумк|рюкзак|\bbag\b|"
                                r"чохол|напульсник|кепк|футболк|протектор|оренд|прокат|тримаєш|надамо|надаємо|"
                                r"візьміть|власн", re.I)
CERTIFICATE = re.compile(r"сертиф|номінал|voucher|certificate|gift card", re.I)
TEASER = re.compile(r"цінн\w*\s+приз\w*|секретн\w*\s+приз\w*|супер\s?приз\w*", re.I)
AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d{1,6})\s*(тис\.?|тисяч\w*|k\b|к\b)?\s*"
                    r"(₴|грн|гривень|гривні|гривня|uah\b|hrn\b)?", re.I)
PLACES = [
    (1, re.compile(r"🥇|(?<!\d)1\s*(?:-?е|-?ше)?\s*місц|перш\w*\s+місц|головн\w*\s+приз|1st\b", re.I)),
    (2, re.compile(r"🥈|(?<!\d)2\s*(?:-?е|-?ге)?\s*місц|друг\w*\s+місц|2nd\b", re.I)),
    (3, re.compile(r"🥉|(?<!\d)3\s*(?:-?є|-?тє)?\s*місц|трет\w*\s+місц|3rd\b", re.I)),
]
MODEL_STOP = re.compile(r"[|!,;:()\[\]{}«»\"“”—–]|\s-\s|[^\x00-\u024f\s]")


def find_brand(text):
    best = None
    for name, rx in BRAND_RES:
        m = rx.search(text)
        if m and (best is None or m.start() < best[1].start()):
            best = (name, m)
    return best


def model_after(text, pos):
    """Latin model words right after the brand: 'NOX X-HERO 2026 🔥' -> 'X-HERO 2026'."""
    tail = MODEL_STOP.split(text[pos:], 1)[0]
    words = [w.strip(".-'’") for w in tail.split()]
    return " ".join([w for w in words if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", w)][:6])


def place_of(line, prev):
    for text in (line, prev):
        for place, rx in PLACES:
            if rx.search(text or ""):
                return place
    return None


def money_mentions(line):
    """Prize amounts in UAH on a line, ignoring entry fees and discounts."""
    found = []
    for m in AMOUNT.finditer(line):
        digits, thousands, currency = m.groups()
        if not (currency or thousands):
            continue
        value = int(re.sub(r"\D", "", digits)) * (1000 if thousands else 1)
        if value < 300:
            continue
        start = max(line.rfind(ch, 0, m.start()) for ch in ",;•|") + 1
        clause = line[start:m.end() + 30]
        if FEE_WORDS.search(clause):
            continue
        if MONEY_WORDS.search(clause) or (MONEY_WORDS.search(line) and not FEE_WORDS.search(line)):
            found.append(value)
    return found


def racket_mention(line, in_title):
    """(brand, model) if the line offers a racket as a prize; brand '' when no brand is named."""
    if CERTIFICATE.search(line) or NOT_A_RACKET_PRIZE.search(line):
        return None
    found = find_brand(line)
    if found:
        name, m = found
        if in_title or RACKET_WORD.search(line) or PRIZE_WORDS.search(line):
            return name, model_after(line, m.end())
        return None
    if RACKET_WORD.search(line) and PRIZE_WORDS.search(line):
        return "", ""
    return None


def extract_prizes(name, description):
    """Prizes mentioned in a tournament's title (split on '|') and description lines."""
    parts = [[(seg.strip(), True) for seg in (name or "").split("|") if seg.strip()],
             [(l.strip(), False) for l in (description or "").splitlines() if l.strip()]]
    prizes = []
    for lines in parts:
        prev = ""
        for line, in_title in lines:
            for value in money_mentions(line):
                prizes.append({"kind": "money", "value": value, "text": tidy(line),
                               "place": place_of(line, prev)})
            r = racket_mention(line, in_title)
            if r:
                prizes.append({"kind": "racket", "brand": r[0], "model": r[1], "text": tidy(line),
                               "place": place_of(line, prev) or (1 if in_title else None)})
            prev = line
    teaser = TEASER.search(f"{name}\n{description or ''}")
    if teaser:
        prizes.append({"kind": "teaser", "text": teaser.group(0), "place": 1})
    return dedupe_prizes(prizes)


def dedupe_prizes(prizes):
    money, rackets, other = {}, {}, []
    for p in prizes:
        if p["kind"] == "money":
            if p["value"] not in money or (p["place"] and not money[p["value"]]["place"]):
                money[p["value"]] = p
        elif p["kind"] == "racket":
            have = rackets.get(p["brand"])
            if have is None or len(p["model"].split()) > len(have["model"].split()):
                rackets[p["brand"]] = p
        else:
            other.append(p)
    if len(rackets) > 1:
        rackets.pop("", None)  # "play for a racket" + a named racket = the named one
    result = list(money.values()) + list(rackets.values())
    if not rackets:
        result += other  # a teaser only matters while nothing concrete is named
    return result


# --------------------------------------------------------------------------- racket prices

PRICE_CACHE_VERSION = 3  # bump when matching or note texts change, so old cached prices are ignored
ACCESSORY = re.compile(r"чохол|сумк|рюкзак|обмотк|намотк|овергрип|\bгрип|ручк|протектор|захист|струн|"
                       r"м['’ʼ]?яч|напульсник|віброгас|\bcover\b|\bbag\b|\bgrip\b|protector", re.I)
JUNIOR = re.compile(r"junior|\bjr\b|kids|дитяч", re.I)


def is_padel_racket(name):
    """'Ракетка для паделу ...' / 'Падел ракетка ...' yes; covers, grips, balls and tennis rackets no."""
    n = name.strip().lower()
    return (re.match(r"(?:падел\w*\s+|padel\s+)?(?:ракетк|racket)", n) is not None
            and re.search(r"падел|падл|\bpadel\b", n) is not None
            and not ACCESSORY.search(n))


SYNONYMS ={"cmf": "comfort", "control": "ctrl", "edition": "edt", "lite": "light"}
NEUTRAL = {"padel", "racket", "pala", "limited", "by", "the", "new", "and", "blue", "red", "white", "black",
           "grey", "gray", "pink", "green", "yellow", "orange", "purple", "gold", "silver", "navy"}


def model_key(text):
    """Model words (lowercase, synonyms applied, brand/colour/SKU words dropped) and the model year."""
    s = re.sub(r"[‐‑‒–—_/\\-]", " ", text.lower())
    tokens, year = set(), None
    for tok in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", s):
        tok = SYNONYMS.get(tok, tok)
        if tok in NEUTRAL or tok in BRAND_WORDS:
            continue
        if re.fullmatch(r"20[1-3]\d", tok):
            year = int(tok)
        elif re.fullmatch(r"1[5-9]|2\d|3[0-5]", tok):
            year = 2000 + int(tok)
        elif re.fullmatch(r"\d{5,}", tok) or (len(tok) >= 6 and re.search(r"\d", tok) and re.search(r"[a-z]", tok)):
            continue  # shop SKU such as AR2AA3U22 or 150153
        else:
            tokens.add(tok)
    return tokens, year


def shop_product(shop, name, url, regular, current, in_stock):
    name = html.unescape(re.sub(r"<[^>]+>", "", name)).strip()
    brand = find_brand(name)
    tokens, year = model_key(name)
    return {"shop": shop, "name": name, "url": html.unescape(url), "regular": regular,
            "current": min(current, regular), "in_stock": in_stock,
            "brand": brand[0] if brand else "", "tokens": tokens, "year": year}


def _int(text):
    return int(re.sub(r"\D", "", text))


def search_padel_tennis(query):
    page = http("https://padel-tennis.com.ua/index.php",
                {"route": "product/search", "search": query, "limit": 100})
    products = []
    for chunk in page.split('<div class="caption')[1:]:
        link = re.search(r'class="product-name"><a href="([^"]+)">(.*?)</a>', chunk, re.S)
        price = re.search(r'data-price-value="(\d+)" data-special-value="(\d*)"', chunk)
        stock = re.search(r"stock-status (\w+)", chunk)
        if link and price:
            regular = int(price.group(1))
            current = int(price.group(2)) if price.group(2) else regular
            products.append(shop_product("padel-tennis.com.ua", link.group(2), link.group(1), regular, current,
                                         bool(stock and stock.group(1) == "instock")))
    return products


TENNISGO_CARD = re.compile(r'<a href="([^"]+)" class="products__item-title">(.*?)</a>(.{0,1500}?)'
                           r'<span class="products__item-price">(.*?)</span>', re.S)


def search_tennisgo(query):
    page = http("https://tennisgo.com.ua/index.php",
                {"route": "product/search", "search": query, "limit": 100})
    products = []
    for url, name, middle, price_html in TENNISGO_CARD.findall(page):
        prices = [_int(p) for p in re.findall(r"(\d[\d \u00a0]*)\s*грн", price_html)]
        if not prices:
            continue
        old = re.search(r"<u>\s*(\d[\d \u00a0]*)\s*грн", price_html)
        regular = _int(old.group(1)) if old else max(prices)
        products.append(shop_product("tennisgo.com.ua", name, url, regular, min(prices),
                                     "products__status-success" in middle))
    return products


SHOP_SEARCHES = (search_padel_tennis, search_tennisgo)


def best_price(brand, tokens, year, products):
    """Regular shop price of the racket that best matches the prize description."""
    candidates = []
    for p in products:
        if p["brand"] != brand or not is_padel_racket(p["name"]):
            continue
        if JUNIOR.search(p["name"]) and "junior" not in tokens:
            continue
        if not tokens <= p["tokens"]:
            continue
        extra = len(p["tokens"] - tokens)  # words the prize didn't mention (Light, Team, Carbon...)
        ydiff = 0 if year is None else (0.5 if p["year"] is None else abs(year - p["year"]))
        candidates.append((extra, ydiff, p))
    if not candidates:
        return None
    extra = min(c[0] for c in candidates)
    group = [c for c in candidates if c[0] == extra]
    ydiff = min(c[1] for c in group)
    group = [c[2] for c in group if c[1] == ydiff]
    ref = min(group, key=lambda p: (p["regular"], not p["in_stock"]))
    sale = min(p["current"] for p in group)
    if not tokens:
        note = "модель не вказано: найдешевша ракетка бренду"
    elif extra:
        note = "версію не вказано: найдешевша відповідна"
    elif year and ydiff >= 1 and ref["year"]:
        note = f"ціна версії {ref['year']} року"
    else:
        note = ""
    return {"value": ref["regular"], "sale": sale if sale < ref["regular"] else None,
            "exact": bool(tokens) and extra == 0, "product": ref["name"], "shop": ref["shop"],
            "url": ref["url"], "in_stock": any(p["in_stock"] for p in group), "note": note}


class Pricer:
    """Looks up racket prices at the shops; results are cached in the store's "prices" document."""

    def __init__(self, cache_days, now, store, searches=SHOP_SEARCHES):
        self.store = store
        stored = store.get("prices", {}) or {}
        self.cache = stored.get("items", {}) if stored.get("version") == PRICE_CACHE_VERSION else {}
        self.max_age = dt.timedelta(days=cache_days)
        self.now = now
        self.searches = searches
        self.results = {}

    def _search(self, query):
        if query not in self.results:
            products, complete = [], True
            for search in self.searches:
                try:
                    products += search(query)
                except Exception as e:  # one shop being down shouldn't stop the run
                    complete = False
                    log.warning("price search %r failed at %s: %s", query, search.__name__, e)
            self.results[query] = (products, complete)
        return self.results[query]

    def price(self, brand, model):
        tokens, year = model_key(model)
        key = f"{brand}|{' '.join(sorted(tokens))}|{year or ''}"
        hit = self.cache.get(key)
        if hit and self.now - dt.datetime.fromisoformat(hit["checked"]) < self.max_age:
            return hit["result"]
        first = next((w for w in model.split() if len(w) >= 2 and not w.isdigit()), "")
        products, complete = self._search(f"ракетка {brand} {first}".strip())  # shops match every word
        result = best_price(brand, tokens, year, products)
        if result or complete:  # don't remember a miss caused by a shop being down
            self.cache[key] = {"checked": self.now.isoformat(), "prize": f"{brand} {model}".strip(),
                               "result": result}
        return result

    def save(self):
        self.store.put("prices", {"version": PRICE_CACHE_VERSION, "items": self.cache})


# --------------------------------------------------------------------------- assessment

def assess(t, pricer, threshold):
    """('yes' | 'maybe' | 'no', best priced prize or None, notes about prizes that couldn't be valued)."""
    priced, unknown = [], []
    for p in extract_prizes(t.get("name") or "", t.get("description") or ""):
        if p["kind"] == "money":
            priced.append({"value": p["value"], "worth": fmt_uah(p["value"]), "label": p["text"], "price": None})
        elif p["kind"] == "racket":
            if not p["brand"]:
                unknown.append("ракетка, модель не вказано")
                continue
            label = " ".join(x for x in ("Ракетка", p["brand"], p["model"]) if x)
            r = pricer.price(p["brand"], p["model"])
            if not r:
                unknown.append(f"{label}: ціну в магазинах не знайдено")
                continue
            # "від" = version not named (cheapest match); "≈" = priced from another model year
            worth = ("від " if not r["exact"] else "≈ " if r["note"] else "") + fmt_uah(r["value"])
            if not r["exact"] and r["value"] < threshold:
                unknown.append(f"{label}, {worth}")  # the exact model may well cost more
            else:
                priced.append({"value": r["value"], "worth": worth, "label": label, "price": r})
        else:
            unknown.append(f"приз ще не названо ({p['text']})")
    best = max(priced, key=lambda x: x["value"]) if priced else None
    if best and best["value"] >= threshold:
        return "yes", best, []
    return ("maybe" if unknown else "no"), None, unknown


def build_entries(tournaments, cfg, now, pricer, clubs, previous):
    entries, statuses = [], {}
    for t in tournaments:
        start = start_time(t)
        if start <= now:
            continue
        status, best, unknown = assess(t, pricer, cfg["min_prize_uah"])
        prize = best["label"] if best else "; ".join(unknown)
        worth = best["worth"] if best else ""
        statuses[str(t["id"])] = {"status": status, "prize": prize, "worth": worth}
        full = (t.get("playersCount") or 0) >= (t.get("maxParticipants") or 10 ** 9)
        if status == "no" or (status == "maybe" and not cfg["show_maybe"]) or (full and not cfg["include_full"]):
            continue
        before = previous.get(str(t["id"]))
        end = (t.get("timeTo") or "")[:5]
        entries.append({
            "id": t["id"],
            "url": TOURNAMENT_LINK.format(id=t["id"]),
            "start": start.isoformat(),
            "when": f"{fmt_day(start)}, {start:%H:%M}" + (f"–{end}" if end else ""),
            "name": display_name(t.get("name") or ""),
            "club": clubs.get(t["id"], ""),
            "fee": t.get("cost"),
            "players": t.get("playersCount") or 0,
            "max_players": t.get("maxParticipants"),
            "full": full,
            "waitlist": t.get("waitlistCount") or 0,
            "pairs": t.get("enrollment") == "double",
            "who": who_can_play(t.get("name") or ""),
            "status": status,
            "value": best["value"] if best else None,
            "worth": worth,
            "prize": prize,
            "price": best["price"] if best else None,
            # nothing is "new" on the very first run
            "new": bool(previous) and status == "yes" and (before is None or before.get("status") != "yes"),
            # a different prize counts as a change; a shop changing a racket's price doesn't
            "changed": bool(before) and before.get("status") == status and before.get("prize") != prize,
        })
    entries.sort(key=lambda e: e["start"])
    return entries, statuses


# --------------------------------------------------------------------------- reports

def tags(e):
    return [x for x in ("парний" if e["pairs"] else "", e["who"]) if x]


def fee(e):
    return fmt_uah(e["fee"]) if e["fee"] else "—"


def plural(n, one, few, many):
    """Ukrainian plural: 1 турнір, 3 турніри, 5 турнірів, 11 турнірів, 21 турнір."""
    tens, ones = n % 100, n % 10
    if ones == 1 and tens != 11:
        word = one
    elif 2 <= ones <= 4 and not 12 <= tens <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def tournaments_count(n):
    return plural(n, "турнір", "турніри", "турнірів")


def new_count(n):
    return plural(n, "новий", "нові", "нових")


def counts(entries):
    yes = [e for e in entries if e["status"] == "yes"]
    return yes, [e for e in entries if e["status"] == "maybe"], sum(e["new"] for e in yes)


def price_note(e):
    p = e["price"]
    if not p:
        return ""
    bits = [p["product"] + " у " + p["shop"]]
    if p["sale"]:
        bits.append("зі знижкою " + fmt_uah(p["sale"]))
    if p["note"]:
        bits.append(p["note"])
    if not p["in_stock"]:
        bits.append("немає в наявності")
    return "; ".join(bits)


def render_markdown(entries, cfg, now):
    yes, maybe, new = counts(entries)
    limit = fmt_uah(cfg["min_prize_uah"])

    def cell(text):
        return str(text).replace("|", "/").replace("\n", " ")

    out = [f"# PADO {city_name(cfg)}: призи від {limit}", "",
           f"Оновлено {fmt_day(now)}, {now:%H:%M}. {tournaments_count(len(yes))}"
           + (f", {new_count(new)}" if new else "") + ".", ""]
    if yes:
        out += ["| Коли | Турнір | Клуб | Приз | Вартість | Внесок |", "|---|---|---|---|---|---|"]
        for e in yes:
            mark = " 🆕" if e["new"] else (" ✏️" if e["changed"] else "")
            name = f"[{cell(e['name'])}]({e['url']})" + (f" ({', '.join(tags(e))})" if tags(e) else "") + mark
            worth = f"[{e['worth']}]({e['price']['url']})" if e["price"] else e["worth"]
            out.append(f"| {e['when']} | {name} | {cell(e['club'])} | {cell(e['prize'])} | {worth} | {fee(e)} |")
    else:
        out.append(f"Зараз немає турнірів із призами від {limit}.")
    if maybe:
        out += ["", f"## Можливо, теж від {limit}", "",
                "| Коли | Турнір | Клуб | Що відомо | Внесок |", "|---|---|---|---|---|"]
        for e in maybe:
            name = f"[{cell(e['name'])}]({e['url']})" + (f" ({', '.join(tags(e))})" if tags(e) else "")
            out.append(f"| {e['when']} | {name} | {cell(e['club'])} | {cell(e['prize'])} | {fee(e)} |")
    out += ["", "Вартість: сертифікати й гроші — як указав організатор; ракетки — за звичайною ціною в "
                "padel-tennis.com.ua або tennisgo.com.ua («від» — точну версію не вказано, тож це найдешевша "
                "відповідна)."]
    return "\n".join(out) + "\n"


HTML_PAGE = """<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Призи PADO</title>
<style>
:root {{ --bg:#fafaf9; --fg:#1c1917; --muted:#78716c; --line:#e7e5e4; --card:#fff; --accent:#0f766e; --new:#fef3c7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1917; --fg:#f5f5f4; --muted:#a8a29e; --line:#44403c;
  --card:#292524; --accent:#5eead4; --new:#422006; }} }}
body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
  font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width:1100px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:32px 0 8px; }}
p.meta, p.foot {{ color:var(--muted); margin:0 0 16px; }} p.foot {{ margin-top:24px; font-size:13px; }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
tr:last-child td {{ border-bottom:0; }} tr.new td {{ background:var(--new); }}
td.num {{ white-space:nowrap; text-align:right; }} td.when {{ white-space:nowrap; }}
a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.tag {{ display:inline-block; font-size:11px; padding:1px 6px; margin-left:6px; border-radius:99px;
  border:1px solid var(--line); color:var(--muted); }}
.note {{ display:block; color:var(--muted); font-size:12px; }}
</style></head><body><main>
<h1>PADO {city}: призи від {limit}</h1>
<p class="meta">Оновлено {updated}. {summary}</p>
{body}
<p class="foot">Вартість: сертифікати й гроші — як указав організатор; ракетки — за звичайною ціною в
padel-tennis.com.ua або tennisgo.com.ua («від» — точну версію не вказано, тож це найдешевша відповідна).</p>
</main></body></html>
"""


def render_html(entries, cfg, now):
    yes, maybe, new = counts(entries)
    esc = html.escape

    def name_cell(e):
        badges = "".join(f'<span class="tag">{esc(t)}</span>' for t in tags(e))
        badges += '<span class="tag">нове</span>' if e["new"] else ('<span class="tag">змінено</span>' if e["changed"] else "")
        return f'<a href="{esc(e["url"])}">{esc(e["name"])}</a>{badges}'

    def worth_cell(e):
        if not e["price"]:
            return esc(e["worth"])
        return f'<a href="{esc(e["price"]["url"])}" title="{esc(price_note(e))}">{esc(e["worth"])}</a>'

    parts = []
    if yes:
        rows = "".join(
            f'<tr{" class=new" if e["new"] else ""}><td class="when">{esc(e["when"])}</td><td>{name_cell(e)}</td>'
            f'<td>{esc(e["club"])}</td><td>{esc(e["prize"])}'
            + (f'<span class="note">{esc(price_note(e))}</span>' if e["price"] else "")
            + f'</td><td class="num">{worth_cell(e)}</td><td class="num">{esc(fee(e))}</td></tr>' for e in yes)
        parts.append('<div class="wrap"><table><tr><th>Коли</th><th>Турнір</th><th>Клуб</th><th>Приз</th>'
                     f'<th>Вартість</th><th>Внесок</th></tr>{rows}</table></div>')
    else:
        parts.append(f"<p>Зараз немає турнірів із призами від {esc(fmt_uah(cfg['min_prize_uah']))}.</p>")
    if maybe:
        rows = "".join(
            f'<tr><td class="when">{esc(e["when"])}</td><td>{name_cell(e)}</td><td>{esc(e["club"])}</td>'
            f'<td>{esc(e["prize"])}</td><td class="num">{esc(fee(e))}</td></tr>'
            for e in maybe)
        parts.append(f'<h2>Можливо, теж від {esc(fmt_uah(cfg["min_prize_uah"]))}</h2>'
                     '<div class="wrap"><table><tr><th>Коли</th><th>Турнір</th><th>Клуб</th>'
                     f"<th>Що відомо</th><th>Внесок</th></tr>{rows}</table></div>")
    summary = tournaments_count(len(yes)) + (f", {new_count(new)}" if new else "") + "."
    return HTML_PAGE.format(city=esc(city_name(cfg)), limit=esc(fmt_uah(cfg["min_prize_uah"])),
                            updated=f"{fmt_day(now)}, {now:%H:%M}", summary=summary, body="\n".join(parts))


# --------------------------------------------------------------------------- Telegram

TELEGRAM_LIMIT = 4000  # Telegram allows 4096 characters per message


def pack(blocks, limit=TELEGRAM_LIMIT):
    """Join blocks with blank lines into as few messages as possible, each at most `limit` characters."""
    pieces = []
    for block in blocks:  # a block that's too long on its own is split by lines
        if len(block) <= limit:
            pieces.append(block)
            continue
        part = ""
        for line in block.split("\n"):
            if part and len(part) + len(line) + 1 > limit:
                pieces.append(part)
                part = ""
            part = f"{part}\n{line}" if part else line[:limit]
        pieces.append(part)
    messages, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) + 2 > limit:
            messages.append(current)
            current = ""
        current = f"{current}\n\n{piece}" if current else piece
    return messages + ([current] if current else [])


def news(entries):
    """Listed tournaments that are new on the list or whose prize changed since the previous run."""
    return [e for e in entries if e["status"] == "yes" and (e["new"] or e["changed"])]


def render_telegram(entries, cfg, only_news=False, updated=None):
    """Telegram messages (HTML parse mode): the whole list, or with only_news just what's new or changed.

    `updated` (a datetime) adds when the list was checked to the full list's header."""
    yes, maybe, new = counts(entries)
    esc = html.escape
    title = f"🎾 <b>PADO {esc(city_name(cfg))}: призи від {esc(fmt_uah(cfg['min_prize_uah']))}</b>\n"
    if only_news:
        shown = news(entries)
        changed = len(shown) - new
        what = ", ".join(x for x in (new_count(new) if new else "", f"{changed} зі зміненим призом" if changed else "") if x)
        blocks = [title + f"{what} · усього у списку {len(yes)}"]
    else:
        shown = yes
        checked = f" · перевірено {fmt_day(updated)}, {updated:%H:%M}" if updated else ""
        blocks = [title + tournaments_count(len(yes)) + (f", {new_count(new)}" if new else "") + checked]
    for e in shown:
        mark = "🆕 " if e["new"] else ("✏️ " if e["changed"] else "")
        worth = f'<a href="{esc(e["price"]["url"])}">{esc(e["worth"])}</a>' if e["price"] else esc(e["worth"])
        extra = "".join(f" · {esc(t)}" for t in tags(e))
        blocks.append(f"{mark}<b>{esc(e['when'])}</b> · {esc(e['club'])}\n"
                      f'<a href="{esc(e["url"])}">{esc(e["name"])}</a>\n'
                      f"🏆 {worth} — {esc(e['prize'])}\n"
                      f"💳 {esc(fee(e))}{extra}")
    if only_news:
        return pack(blocks)
    if not yes:
        blocks.append(f"Зараз немає турнірів із призами від {fmt_uah(cfg['min_prize_uah'])}.")
    if maybe:
        blocks.append("❓ <b>Можливо, теж варто уваги</b>\n" + "\n".join(
            f'• {esc(e["when"])} <a href="{esc(e["url"])}">{esc(tidy(e["name"], 60))}</a>: {esc(e["prize"])}'
            for e in maybe))
    return pack(blocks)


# --------------------------------------------------------------------------- one check

def run_check(cfg, now, store):
    """Fetch tournaments, value their prizes and compare with the previous check.

    Saves the caches, the state used to spot news next time and the "latest" list in `store`.
    Returns (entries, number of tournaments checked)."""
    tournaments = fetch_tournaments(cfg, now)
    previous = (store.get("state", {}) or {}).get("tournaments", {})
    pricer = Pricer(cfg["price_cache_days"], now, store)
    entries, statuses = build_entries(tournaments, cfg, now, pricer, club_names(tournaments, store), previous)
    pricer.save()
    store.put("state", {"updated": now.isoformat(), "tournaments": statuses})
    store.put("latest", {"updated": now.isoformat(), "checked": len(tournaments), "tournaments": entries})
    return entries, len(tournaments)


# --------------------------------------------------------------------------- main

def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "run.log")
    if os.path.exists(path) and os.path.getsize(path) > 512_000:
        os.replace(path, path + ".1")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    log.addHandler(handler)
    log.addHandler(console)
    log.setLevel(logging.INFO)


def main(argv=None):
    ap = argparse.ArgumentParser(description="List upcoming PADO tournaments with valuable prizes.")
    ap.add_argument("--quiet", action="store_true", help="don't print the report")
    args = ap.parse_args(argv)
    setup_logging()
    cfg, now = load_config(), dt.datetime.now(KYIV)
    try:
        entries, checked = run_check(cfg, now, FileStore(DATA_DIR))
    except Exception:
        log.exception("The check failed")
        return 1

    report = render_markdown(entries, cfg, now)
    write_text(os.path.join(OUT_DIR, "prizes.md"), report)
    write_text(os.path.join(OUT_DIR, "prizes.html"), render_html(entries, cfg, now))
    save_json(os.path.join(OUT_DIR, "prizes.json"), {"updated": now.isoformat(), "tournaments": entries})
    yes, maybe, new = counts(entries)
    log.info("%d tournaments checked; %d with prizes >= %d UAH (%d new), %d maybe",
             checked, len(yes), cfg["min_prize_uah"], new, len(maybe))
    if not args.quiet:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
