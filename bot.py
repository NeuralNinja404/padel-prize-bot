"""Telegram bot for PADO prize tournaments.

Friends press Start to subscribe and get the current list, /list shows it any time, /stop turns
alerts off, and once a day subscribers get a message about tournaments that are new on the list
(or whose prize changed). Quiet days send nothing.

Runs on Vercel through app.py. Storage and Telegram are passed in, so the logic is easy to test.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pado_prizes as core

log = logging.getLogger("pado_bot")

# Texts friends see. {city_in} = "у Києві", {city} = "Київ", {limit} = "5 000 ₴" (from config.json).
COMMANDS = [
    ("list", "Турніри з призами від {limit}"),
    ("start", "Увімкнути сповіщення про нові турніри"),
    ("stop", "Вимкнути сповіщення"),
    ("help", "Що вміє цей бот"),
]
DESCRIPTION = ("Турніри з паделу {city_in} на PADO з призами від {limit}: ракетки, сертифікати, гроші. "
               "Запустіть бота, щоб отримати список і повідомлення про кожен новий такий турнір.")
SHORT_DESCRIPTION = "Турніри з паделу {city_in} на PADO з призами від {limit}"
WELCOME = ("👋 Я стежу за турнірами з паделу {city_in} на PADO і відбираю ті, де приз коштує від {limit}: "
           "ракетки, сертифікати, гроші.\n\n"
           "• Раз на день я напишу, якщо з’явиться новий такий турнір. Якщо нічого нового, повідомлень не буде.\n"
           "• /list — актуальний список будь-коли.\n"
           "• /stop — вимкнути сповіщення.\n\n"
           "Вартість призів орієнтовна, за цінами магазинів. Перед реєстрацією перевірте сторінку турніру.")
HELP = "/list — турніри з призами від {limit}\n/start — увімкнути сповіщення\n/stop — вимкнути сповіщення"
STOPPED = "🔕 Сповіщення вимкнено. Надішліть /start, щоб увімкнути знову."
TOO_MANY = "⏳ Забагато запитів. Спробуйте пізніше."
LIMIT_CHAT = "⏳ Сьогодні ви вже кілька разів змінювали підписку. Спробуйте завтра."
LIMIT_ALL = "⏳ Сьогодні вже забагато нових підписок. Спробуйте завтра."

# Limits that keep the bot inside Vercel's free plan however many people use it.
# In memory (per running instance; stops bursts, resets when Vercel recycles the instance):
PER_CHAT_PER_HOUR = 10      # commands one chat may send per hour
PER_CHAT_PER_DAY = 30       # ...and per day
EVERYONE_PER_MINUTE = 60    # all chats together
EVERYONE_PER_DAY = 500
WARN_EVERY = 3600.0         # "too many requests" is said at most once an hour per chat
# Stored with the subscribers (survives restarts, costs no extra storage operations). Subscribing and
# unsubscribing are what spend the plan's writes, so these caps keep writes under its monthly allowance:
CHANGES_PER_CHAT_PER_DAY = 3      # subscribe/unsubscribe per chat per day (/stop always works)
NEW_SUBSCRIPTIONS_PER_DAY = 30    # for the whole bot
SEND_RATE = 20              # alert messages per second, below Telegram's ~30 for bulk sending
SEND_WORKERS = 8            # alerts sent in parallel
MINUTE, HOUR, DAY = 60.0, 3600.0, 86400.0


def webhook_secret(token):
    """The secret Telegram sends with every update, so the webhook ignores anyone else. Derived from the token."""
    return hashlib.sha256(f"pado-webhook:{token}".encode()).hexdigest()[:48]


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"Telegram error {code}: {description}")
        self.code, self.description, self.retry_after = code, description, retry_after


class Telegram:
    """Minimal Bot API client. The token is part of the URL, so it never goes into errors or logs."""

    def __init__(self, token, timeout=20):
        self.token, self.timeout = token, timeout

    def call(self, method, **params):
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(params).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                reply = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                reply = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                reply = {"error_code": e.code, "description": ""}
        except (urllib.error.URLError, OSError) as e:
            raise TelegramError(0, type(e).__name__) from None
        if not reply.get("ok"):
            raise TelegramError(reply.get("error_code", 0), reply.get("description", ""),
                                (reply.get("parameters") or {}).get("retry_after"))
        return reply.get("result")

    def send(self, chat_id, text):
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         link_preview_options={"is_disabled": True})


def is_gone(error):
    """Telegram says this chat can't be messaged any more (blocked the bot, deleted account, chat gone)."""
    return error.code == 403 or (error.code == 400 and "chat not found" in error.description.lower())


class CommandLimiter:
    """Caps how often each chat, and everyone together, can use the bot.

    Kept in memory, not in storage (counting in storage would itself spend the writes we're saving). Vercel
    keeps an instance warm while requests keep coming, so a burst is stopped; counters reset when an idle
    instance is recycled. The hard caps on subscription changes (in Bot) are stored and survive that."""

    def __init__(self, per_hour=PER_CHAT_PER_HOUR, per_day=PER_CHAT_PER_DAY,
                 everyone_per_minute=EVERYONE_PER_MINUTE, everyone_per_day=EVERYONE_PER_DAY,
                 warn_every=WARN_EVERY, clock=time.monotonic):
        self.per_hour, self.per_day = per_hour, per_day
        self.everyone_per_minute, self.everyone_per_day = everyone_per_minute, everyone_per_day
        self.warn_every, self.clock = warn_every, clock
        self.lock = threading.Lock()
        self.chats = {}           # chat -> times of its commands within the last day
        self.everyone = deque()   # times of all commands within the last day
        self.warned = {}          # chat -> when it was last told "too many requests"

    def check(self, chat):
        """'ok' to handle the command, 'warn' to say (once an hour) that it's too many, 'drop' to ignore it."""
        now = self.clock()
        with self.lock:
            if len(self.chats) > 10000:  # forget chats that have been quiet for a day
                self.chats = {c: t for c, t in self.chats.items() if t and now - t[-1] < DAY}
            times = self.chats.setdefault(chat, deque())
            while times and now - times[0] >= DAY:
                times.popleft()
            while self.everyone and now - self.everyone[0] >= DAY:
                self.everyone.popleft()
            last_hour = sum(1 for t in times if now - t < HOUR)
            everyone_last_minute = sum(1 for t in self.everyone if now - t < MINUTE)
            if (last_hour < self.per_hour and len(times) < self.per_day
                    and everyone_last_minute < self.everyone_per_minute
                    and len(self.everyone) < self.everyone_per_day):
                times.append(now)
                self.everyone.append(now)
                return "ok"
            last_warned = self.warned.get(chat)
            if last_warned is None or now - last_warned >= self.warn_every:
                self.warned[chat] = now
                return "warn"
            return "drop"


COMMAND_LIMITER = CommandLimiter()  # shared by every request this instance handles


class Pace:
    """Spaces Telegram sends evenly across threads."""

    def __init__(self, per_second):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next - now)
            self.next = max(now, self.next) + self.interval
        if delay:
            time.sleep(delay)


class Bot:
    def __init__(self, telegram, store, cfg, clock=None, limiter=None):
        self.tg, self.store, self.cfg = telegram, store, cfg
        self.clock = clock or (lambda: dt.datetime.now(core.KYIV))
        self.limiter = limiter or COMMAND_LIMITER

    def text(self, template):
        city = core.city_name(self.cfg)
        return template.format(city=city, city_in=self.cfg.get("city_in") or f"у {city}",
                               limit=core.fmt_uah(self.cfg["min_prize_uah"]))

    # ------------------------------------------------------------------ subscribers
    # stored as the "subscribers" document: {"chats": {"<chat id>": {"since": "<iso time>"}}}

    def subscribers(self):
        return list(((self.store.get("subscribers", {}) or {}).get("chats") or {}).keys())

    # The same document keeps today's subscription changes, so the caps survive restarts at no extra cost:
    # "changes": {"day": "2026-11-10", "new": 4, "by_chat": {"<chat id>": 1}}

    @staticmethod
    def _changes_today(doc, today):
        changes = doc.get("changes") or {}
        return changes if changes.get("day") == today else {"day": today, "new": 0, "by_chat": {}}

    def _refusal(self, doc, key, today, subscribing):
        changes = self._changes_today(doc, today)
        if subscribing and (changes.get("by_chat") or {}).get(key, 0) >= CHANGES_PER_CHAT_PER_DAY:
            return "limit_chat"
        if subscribing and changes.get("new", 0) >= NEW_SUBSCRIPTIONS_PER_DAY:
            return "limit_all"
        return None

    def _set_subscribed(self, chat_id, subscribing):
        """Subscribe or unsubscribe one chat on its own request: 'ok', 'already', 'limit_chat' or 'limit_all'.

        Checks the cached subscriber list first, so repeats and refusals cost no storage operation;
        only a real change reads the list fresh and writes it."""
        key, now = str(chat_id), self.clock()
        today = now.date().isoformat()
        cached = self.store.get("subscribers", {}, fresh=False) or {}
        if (key in (cached.get("chats") or {})) == subscribing:
            return "already"
        refusal = self._refusal(cached, key, today, subscribing)
        if refusal:
            return refusal
        outcome = []

        def change(doc):
            outcome[:] = ["ok"]
            chats = doc.setdefault("chats", {})
            if (key in chats) == subscribing:
                outcome[:] = ["already"]
                return doc
            refusal = self._refusal(doc, key, today, subscribing)
            if refusal:
                outcome[:] = [refusal]
                return doc
            if subscribing:
                chats[key] = {"since": now.isoformat()}
            else:
                chats.pop(key, None)
            changes = self._changes_today(doc, today)
            changes["new"] = changes.get("new", 0) + (1 if subscribing else 0)
            changes.setdefault("by_chat", {})[key] = changes["by_chat"].get(key, 0) + 1
            doc["changes"] = changes
            return doc
        self.store.update("subscribers", change, default={"chats": {}})
        return outcome[0] if outcome else "ok"

    def subscribe(self, chat_id):
        return self._set_subscribed(chat_id, True)

    def unsubscribe(self, chat_id):
        """/stop always works (it only ever reduces messages); it just counts toward the chat's changes."""
        return self._set_subscribed(chat_id, False)

    def unsubscribe_many(self, chat_ids):
        keys = {str(c) for c in chat_ids}

        def change(doc):
            chats = doc.setdefault("chats", {})
            for key in keys:
                chats.pop(key, None)
            return doc
        self.store.update("subscribers", change, default={"chats": {}})  # one write for all of them

    # ------------------------------------------------------------------ the list

    def check(self, now=None):
        return core.run_check(self.cfg, now or self.clock(), self.store)

    def list_messages(self):
        # The list changes once a day, so a cached copy (up to a minute old) is fine and costs nothing.
        doc = self.store.get("latest", fresh=False)
        if not doc:  # nothing checked yet; setup normally does the first check
            self.check()
            doc = self.store.get("latest") or {}
        updated = dt.datetime.fromisoformat(doc["updated"]) if doc.get("updated") else None
        return core.render_telegram(doc.get("tournaments", []), self.cfg, updated=updated)

    # ------------------------------------------------------------------ incoming updates

    def handle_update(self, update):
        member = update.get("my_chat_member")
        if member:  # someone blocked the bot or removed it from a group
            if (member.get("new_chat_member") or {}).get("status") in ("kicked", "left"):
                key = str(member["chat"]["id"])
                if key in ((self.store.get("subscribers", {}, fresh=False) or {}).get("chats") or {}):
                    self.unsubscribe_many([key])
            return
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat is None:
            return
        command = text.split()[0].lower() if text.startswith("/") else ""
        if "@" in command:  # "/list@some_bot" in a group: only answer when it's addressed to us
            command, addressee = command.split("@", 1)
            username = ((self.store.get("bot", {}, fresh=False) or {}).get("username") or "").lower()
            if username and addressee != username:
                return
        verdict = self.limiter.check(chat)
        if verdict != "ok":
            if verdict == "warn":
                self.send(chat, [TOO_MANY])
            return
        if command == "/start":
            outcome = self.subscribe(chat)
            if outcome in ("ok", "already"):
                self.send(chat, [self.text(WELCOME)] + self.list_messages())
            else:
                self.send(chat, [LIMIT_CHAT if outcome == "limit_chat" else LIMIT_ALL])
        elif command == "/list":
            self.send(chat, self.list_messages())
        elif command == "/stop":
            self.unsubscribe(chat)
            self.send(chat, [STOPPED])
        else:
            self.send(chat, [self.text(HELP)])

    def send(self, chat, messages, pace=None):
        for text in messages:
            if pace:
                pace.wait()
            try:
                self.tg.send(chat, text)
            except TelegramError as e:
                if e.code != 429 or not e.retry_after:
                    raise
                time.sleep(min(int(e.retry_after), 30))  # rate limited: wait as asked, then try once more
                self.tg.send(chat, text)

    # ------------------------------------------------------------------ daily check

    def daily(self, now=None, broadcast=True):
        """Check PADO and message every subscriber about what's new. Returns a summary for the logs.

        broadcast=False refreshes the stored list without messaging anyone (e.g. after changing texts,
        when every prize label would otherwise look "changed")."""
        entries, checked = self.check(now)
        yes, maybe, _ = core.counts(entries)
        fresh = core.news(entries)
        chats = self.subscribers()
        summary = {"checked": checked, "listed": len(yes), "maybe": len(maybe), "news": len(fresh),
                   "subscribers": len(chats), "delivered": 0, "removed": 0, "failed": 0, "quiet": not broadcast}
        if not fresh or not broadcast:
            return summary
        messages = core.render_telegram(entries, self.cfg, only_news=True)
        pace = Pace(SEND_RATE)

        def deliver(chat):
            try:
                self.send(chat, messages, pace)
                return chat, None
            except TelegramError as e:
                return chat, e
            except Exception as e:  # one odd failure shouldn't stop the others
                return chat, TelegramError(0, type(e).__name__)

        gone = []
        with ThreadPoolExecutor(max_workers=SEND_WORKERS) as pool:
            for chat, error in pool.map(deliver, chats):
                if error is None:
                    summary["delivered"] += 1
                elif is_gone(error):
                    gone.append(chat)
                else:
                    summary["failed"] += 1
                    log.warning("couldn't message a subscriber: %s", error)
        if gone:
            self.unsubscribe_many(gone)
        summary["removed"] = len(gone)
        return summary

    # ------------------------------------------------------------------ one-off setup

    def setup(self, base_url, now=None):
        """Point Telegram at the webhook, set the command menu and descriptions, and do the first check."""
        me = self.tg.call("getMe") or {}
        self.store.put("bot", {"username": me.get("username", "")})
        webhook = f"{base_url.rstrip('/')}/api/telegram"
        self.tg.call("setWebhook", url=webhook, secret_token=webhook_secret(self.tg.token),
                     allowed_updates=["message", "my_chat_member"])
        self.tg.call("setMyCommands", commands=[{"command": c, "description": self.text(d)} for c, d in COMMANDS])
        self.tg.call("setMyDescription", description=self.text(DESCRIPTION))
        self.tg.call("setMyShortDescription", short_description=self.text(SHORT_DESCRIPTION))
        result = {"bot": me.get("username", ""), "webhook": webhook}
        if not self.store.get("latest"):
            entries, checked = self.check(now)  # nothing counts as news on the very first check
            result.update(checked=checked, listed=len(core.counts(entries)[0]))
        return result
