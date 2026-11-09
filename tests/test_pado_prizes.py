"""Offline tests for pado_prizes.py, built from real PADO tournament texts (November 2026).

Run:  python3 -m unittest discover -s tests -v
"""
import contextlib
import datetime as dt
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pado_prizes as p  # noqa: E402


def kinds(name, description=""):
    out = []
    for x in p.extract_prizes(name, description):
        if x["kind"] == "money":
            out.append(("money", x["value"], x["place"]))
        elif x["kind"] == "racket":
            out.append(("racket", x["brand"], x["model"]))
        else:
            out.append(("teaser",))
    return out


class MoneyPrizes(unittest.TestCase):
    def test_certificate_in_description(self):
        self.assertEqual(kinds("CULT PADEL CLUB | STREET ARENA | D+/C-",
                               "Збір Cult Padel Club | Winners court\n"
                               "🏆За 1 місце - сертифікат номіналом 7000 ₴ від BABOLAT"),
                         [("money", 7000, 1)])

    def test_certificate_in_title(self):
        self.assertEqual(kinds("CULT PADEL TOURNAMENT | СЕРТИФІКАТ BABOLAT 15000 ₴ 🏆 | D+/C-"),
                         [("money", 15000, None)])

    def test_places_on_the_line_before(self):
        self.assertEqual(kinds("CULT PADEL CLUB | SVITO Padel | DUO D+/C-",
                               "🥇1 місце\nВечеря номіналом 1 500 ₴ в закладах OG Burger 🍔\n"
                               "🥈2 місце\nСертифікат номіналом 2 000 ₴ на оренду корта в SVITO Padel 🎾\n"
                               "🥉 3 місце\nВишуканий презент у вигляді вина JP. Chenet 🥂"),
                         [("money", 1500, 1), ("money", 2000, 2)])

    def test_entry_fees_are_not_prizes(self):
        for line in ("💳 Вартість: 1300 грн", "💰 1300 грн", "1700 uah", "Ціна 300 грн (корт +м'ячі )",
                     "Знижка 200 грн для членів клубу"):
            with self.subTest(line=line):
                self.assertEqual(kinds("Americano D/D+", line), [])

    def test_fee_and_prize_on_one_line(self):
        self.assertEqual(kinds("Cup", "Вартість участі 1300 грн, переможець отримує сертифікат на 3000 грн"),
                         [("money", 3000, None)])


class RacketPrizes(unittest.TestCase):
    def test_model_in_title(self):
        self.assertEqual(kinds("Padel Marathon D/D+ |🥇Ракетка NOX X-HERO"), [("racket", "NOX", "X-HERO")])

    def test_brand_in_title_without_the_word_racket(self):
        self.assertEqual(kinds("НЕДІЛЯ | 16-18 | С-/С | ADIDAS METALBONE Control 2026"),
                         [("racket", "Adidas", "METALBONE Control 2026")])

    def test_most_specific_mention_wins(self):
        self.assertEqual(kinds("💎ГРАЄМО НА РАКЕТКУ BULLPADEL!🏆￼13.11💎 20:00-22:00💎",
                               "🎾🔥 SMASH CLUB — ГРАЄМО НА РАКЕТКУ BULLPADEL! 🏆\n"
                               "🏆 Головний приз — BULLPADEL IONIC CONTROL 25! 🎁"),
                         [("racket", "Bullpadel", "IONIC CONTROL 25")])

    def test_racket_without_brand(self):
        self.assertEqual(kinds("Граємо на ракетку! LADNA Girls Tournament: D-/D/D+"), [("racket", "", "")])

    def test_brand_certificate_is_money_not_racket(self):
        self.assertEqual(kinds("Cup", "🏆 сертифікат номіналом 7000 ₴ від BABOLAT"), [("money", 7000, None)])

    def test_racket_words_that_are_not_prizes(self):
        self.assertEqual(kinds("ПОНЕДІЛОК 18-20 | D-/D | KYIV PADEL CLUB",
                               "🎁 Призи за 1,2,3 місце\n🏓 Намотка на ракетку, гель для рук, аптечка"), [])
        self.assertEqual(kinds("Winners Court",
                               "Тут неважливо, чи ти граєш уперше, чи вже впевнено тримаєш ракетку."), [])
        self.assertEqual(kinds("Ricochet Padel Evening | Racket Sports Park Nyvky 🏆"), [])
        self.assertEqual(kinds("PALA Start 🎾 (Beginners, D-)", "подарунки для переможців 🎁"), [])

    def test_teaser_until_the_prize_is_named(self):
        self.assertEqual(kinds("Padel Marathon D- | 🥇Цінний приз :)", "🏆 Призи та медалі переможцям"),
                         [("teaser",)])
        self.assertEqual(kinds("Padel Marathon | 🥇Цінний приз :)",
                               "🥇 1 місце - ракетка для паделу Bullpadel IONIC POWER 26"),
                         [("racket", "Bullpadel", "IONIC POWER 26")])


def shop(name, regular, current=None, in_stock=True):
    return p.shop_product("test-shop", name, "https://shop.test/" + name.replace(" ", "-"), regular,
                          current if current is not None else regular, in_stock)


class Pricing(unittest.TestCase):
    def test_model_key_normalises_names(self):
        self.assertEqual(p.model_key("NEURON 02 CMF 27"), ({"neuron", "02", "comfort"}, 2027))
        self.assertEqual(p.model_key("NEURON 02 CMF 27"),
                         p.model_key("Ракетка для падела Bullpadel Neuron 02 Comfort 2027"))
        self.assertEqual(p.model_key("METALBONE Control 2026"),
                         p.model_key("Ракетка для паделу Adidas Metalbone Ctrl 2026"))
        self.assertEqual(p.model_key("Ракетка для паделу Adidas Metalbone Team 2026 AR2AA3U22"),
                         ({"metalbone", "team"}, 2026))

    def test_padel_racket_filter(self):
        self.assertTrue(p.is_padel_racket("Ракетка для паделу Adidas Metalbone 2026"))
        self.assertTrue(p.is_padel_racket("Падел ракетка Siux Fenix"))
        self.assertTrue(p.is_padel_racket("Ракетка для падел тенісу Babolat Stima Vita"))
        self.assertFalse(p.is_padel_racket("Чохол для падел ракетки Babolat Cover Padel 900224/100"))
        self.assertFalse(p.is_padel_racket("Ракетка для великого тенісу Babolat Pure Drive"))

    METALBONES = [
        shop("Ракетка для паделу Adidas Metalbone Ctrl 2026", 22999),
        shop("Ракетка для паделу Adidas Metalbone Carbon CTRL 2026 AR2CA2U23", 14999),
        shop("Ракетка для паделу Adidas Metalbone Team 2026 AR2AA3U22", 11999),
        shop("Ракетка для паделу Adidas Metalbone Superlight AR4CA1U22", 9999),
        shop("Ракетка для паделу Adidas Metalbone Junior", 3999),
        shop("Чохол для ракетки Adidas Metalbone", 899),
    ]

    def price(self, brand, model, products):
        tokens, year = p.model_key(model)
        return p.best_price(brand, tokens, year, products)

    def test_exact_model_beats_longer_names(self):
        r = self.price("Adidas", "METALBONE Control 2026", self.METALBONES)
        self.assertEqual((r["value"], r["exact"]), (22999, True))

    def test_family_only_gives_cheapest_adult_version(self):
        r = self.price("Adidas", "Metalbone", self.METALBONES)
        self.assertEqual((r["value"], r["exact"]), (9999, False))

    def test_other_model_year_is_flagged(self):
        products = [shop("Ракетка для падела Bullpadel Ionic Power 25", 9530),
                    shop("Ракетка для падл-тенісу Bullpadel Ionic Power 2023", 8880, 7999, in_stock=False)]
        r = self.price("Bullpadel", "IONIC POWER 26", products)
        self.assertEqual((r["value"], r["note"]), (9530, "ціна версії 2025 року"))

    def test_sale_price_and_cheapest_shop(self):
        products = [shop("Ракетка для паделу Nox X-Hero Blue 2026", 5230),
                    shop("Ракетка падел Nox X-Hero Blue", 5400, 4300)]
        r = self.price("NOX", "X-HERO", products)
        self.assertEqual((r["value"], r["sale"]), (5230, 4300))

    def test_no_match(self):
        self.assertIsNone(self.price("Bullpadel", "VERTEX 05", self.METALBONES))


class FakePricer:
    def __init__(self, table):
        self.table = table

    def price(self, brand, model):
        return self.table.get((brand, model))


def priced(value, exact=True, note=""):
    return {"value": value, "sale": None, "exact": exact, "product": "x", "shop": "test-shop",
            "url": "https://shop.test/x", "in_stock": True, "note": note}


class Assessment(unittest.TestCase):
    def status(self, name, description="", table=None):
        status, best, _ = p.assess({"name": name, "description": description}, FakePricer(table or {}), 5000)
        return status, best["worth"] if best else None

    def test_statuses(self):
        self.assertEqual(self.status("CULT | СЕРТИФІКАТ BABOLAT 15000 ₴"), ("yes", "15 000 ₴"))
        self.assertEqual(self.status("СУБОТА | ADIDAS ARROW HIT pro edt 2026",
                                     table={("Adidas", "ARROW HIT pro edt 2026"): priced(26499)}),
                         ("yes", "26 499 ₴"))
        self.assertEqual(self.status("Bullpadel IONIC POWER 26 | Marathon",
                                     table={("Bullpadel", "IONIC POWER 26"): priced(9530, note="price of the 2025 version")}),
                         ("yes", "≈ 9 530 ₴"))
        self.assertEqual(self.status("Winners Court | Ракетка Babolat",
                                     table={("Babolat", ""): priced(4000, exact=False)}), ("maybe", None))
        self.assertEqual(self.status("Padel Marathon D- | 🥇Цінний приз :)"), ("maybe", None))
        self.assertEqual(self.status("Girls only D-/D", "🏅 медальки для переможниць"), ("no", None))
        self.assertEqual(self.status("Girls only", "🥇 1 місце - сертифікат на 2000 грн"), ("no", None))


class NewAndChanged(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 9, 10, 0, tzinfo=p.KYIV)

    def tournament(self, tid, name):
        return {"id": tid, "name": name, "description": "", "startDate": "2026-11-14T08:00:00.000Z",
                "timeFrom": "10:00:00", "timeTo": "12:00:00", "timezone": "Europe/Kyiv", "cost": 1300,
                "playersCount": 3, "maxParticipants": 16, "enrollment": "single"}

    def entries(self, previous):
        ts = [self.tournament(1, "A | СЕРТИФІКАТ 15000 ₴"), self.tournament(2, "B | сертифікат 7000 ₴"),
              self.tournament(3, "C | сертифікат 9000 ₴")]
        entries, statuses = p.build_entries(ts, p.DEFAULT_CONFIG, self.NOW, FakePricer({}), {}, previous)
        return {e["id"]: (e["new"], e["changed"]) for e in entries}, statuses

    def test_first_run_marks_nothing_new(self):
        marks, statuses = self.entries({})
        self.assertEqual(marks, {1: (False, False), 2: (False, False), 3: (False, False)})
        self.assertEqual(statuses["1"], {"status": "yes", "prize": "СЕРТИФІКАТ 15000 ₴", "worth": "15 000 ₴"})

    def test_new_and_changed(self):
        # 1: same prize, only its worth moved (a shop price change) -> not a change
        previous = {"1": {"status": "yes", "prize": "СЕРТИФІКАТ 15000 ₴", "worth": "14 000 ₴"},
                    "2": {"status": "maybe", "prize": "prize not named yet", "worth": ""},
                    "3": {"status": "yes", "prize": "сертифікат 5000 ₴", "worth": "5 000 ₴"}}
        marks, _ = self.entries(previous)
        self.assertEqual(marks, {1: (False, False), 2: (True, False), 3: (False, True)})

    def test_started_tournaments_are_skipped(self):
        t = self.tournament(4, "D | сертифікат 8000 ₴")
        t["startDate"], t["timeFrom"] = "2026-11-09T06:00:00.000Z", "08:00:00"
        entries, _ = p.build_entries([t], p.DEFAULT_CONFIG, self.NOW, FakePricer({}), {}, {})
        self.assertEqual(entries, [])


def entry(i, status="yes", name=None, new=False):
    return {"id": i, "status": status, "new": new, "changed": False, "when": "Sat 14 Nov 16:00–18:00",
            "club": "P12 Padel Club", "url": f"https://pado.app/uk/ukraine/kyiv/tournaments/t-{i}",
            "name": name or f"Padel Marathon D/D+ · 🥇Bullpadel NEURON 27 · #{i}", "prize": "Bullpadel NEURON 02 CMF 27",
            "worth": "15 400 ₴" if status == "yes" else "", "fee": 1300, "players": 11, "max_players": 24,
            "full": False, "waitlist": 0, "pairs": False, "who": "",
            "price": {"url": "https://padel-tennis.com.ua/x"} if status == "yes" else None}


class TelegramRendering(unittest.TestCase):
    def test_long_lists_are_split_under_the_limit(self):
        entries = [entry(i) for i in range(40)] + [entry(100 + i, "maybe") for i in range(10)]
        messages = p.render_telegram(entries, p.DEFAULT_CONFIG)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= p.TELEGRAM_LIMIT for m in messages))
        self.assertTrue(messages[0].startswith("🎾 <b>PADO Київ: призи від 5 000 ₴</b>"))
        joined = "\n".join(messages)
        self.assertTrue(all(e["url"] in joined for e in entries))

    def test_names_are_html_escaped(self):
        text = p.render_telegram([entry(1, name="A <b> & B")], p.DEFAULT_CONFIG)[0]
        self.assertIn("A &lt;b&gt; &amp; B", text)

    def test_pack_splits_a_block_that_is_too_long(self):
        block = "\n".join("x" * 90 for _ in range(100))
        self.assertTrue(all(len(m) <= 1000 for m in p.pack(["head", block], limit=1000)))

    def test_news_lists_only_new_and_changed_tournaments(self):
        changed = dict(entry(4), changed=True)
        text = "\n".join(p.render_telegram([entry(1, new=True), entry(2), entry(3, "maybe"), changed],
                                           p.DEFAULT_CONFIG, only_news=True))
        self.assertIn("1 новий, 1 зі зміненим призом · усього у списку 3", text)
        self.assertIn("t-1", text)
        self.assertIn("t-4", text)
        self.assertNotIn("t-2", text)
        self.assertNotIn("Можливо, теж варто уваги", text)

    def test_full_list_says_when_it_was_checked(self):
        text = p.render_telegram([entry(1)], p.DEFAULT_CONFIG,
                                 updated=dt.datetime(2026, 11, 10, 10, 5, tzinfo=p.KYIV))[0]
        self.assertIn("1 турнір · перевірено Вт, 10 листопада, 10:05", text)


def tournament(tid, name, start="2026-11-14T08:00:00.000Z"):
    return {"id": tid, "name": name, "description": "", "startDate": start, "timeFrom": "10:00:00",
            "timeTo": "12:00:00", "timezone": "Europe/Kyiv", "cost": 1300, "playersCount": 3,
            "maxParticipants": 16, "enrollment": "single", "customLocation": "P12 Padel Club"}


class Storage(unittest.TestCase):
    def test_file_store_get_put_update(self):
        with tempfile.TemporaryDirectory() as folder:
            store = p.FileStore(folder)
            self.assertEqual(store.get("missing", {"a": 1}), {"a": 1})
            store.put("doc", {"x": 1})
            self.assertEqual(store.update("doc", lambda d: dict(d, y=2)), {"x": 1, "y": 2})
            self.assertEqual(store.get("doc"), {"x": 1, "y": 2})

    def test_run_check_saves_state_and_spots_news_next_time(self):
        now = dt.datetime(2026, 11, 9, 10, 0, tzinfo=p.KYIV)
        first = [tournament(1, "A | СЕРТИФІКАТ 15000 ₴"), tournament(2, "B | medals")]
        second = first + [tournament(3, "C | сертифікат 7000 ₴")]
        with tempfile.TemporaryDirectory() as folder:
            store = p.FileStore(folder)
            with mock.patch.object(p, "fetch_tournaments", lambda cfg, now: first):
                entries, checked = p.run_check(p.DEFAULT_CONFIG, now, store)
            self.assertEqual((checked, [e["id"] for e in entries], p.news(entries)), (2, [1], []))
            with mock.patch.object(p, "fetch_tournaments", lambda cfg, now: second):
                entries, checked = p.run_check(p.DEFAULT_CONFIG, now, store)
            self.assertEqual([e["id"] for e in p.news(entries)], [3])
            latest = store.get("latest")
            self.assertEqual((latest["checked"], len(latest["tournaments"])), (3, 2))


class UkrainianTexts(unittest.TestCase):
    def test_plural_forms(self):
        self.assertEqual([p.tournaments_count(n) for n in (1, 2, 5, 11, 14, 21, 22, 25, 101, 111)],
                         ["1 турнір", "2 турніри", "5 турнірів", "11 турнірів", "14 турнірів", "21 турнір",
                          "22 турніри", "25 турнірів", "101 турнір", "111 турнірів"])
        self.assertEqual([p.new_count(n) for n in (1, 3, 7)], ["1 новий", "3 нові", "7 нових"])

    def test_dates_and_racket_labels(self):
        self.assertEqual(p.fmt_day(dt.date(2026, 11, 14)), "Сб, 14 листопада")
        status, best, _ = p.assess({"name": "Winners Court | Ракетка Babolat"},
                                   FakePricer({("Babolat", ""): priced(6399, exact=False)}), 5000)
        self.assertEqual((status, best["label"], best["worth"]), ("yes", "Ракетка Babolat", "від 6 399 ₴"))


if __name__ == "__main__":
    unittest.main()
