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
import time
import urllib.error
import urllib.request

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


class Bot:
    def __init__(self, telegram, store, cfg, clock=None):
        self.tg, self.store, self.cfg = telegram, store, cfg
        self.clock = clock or (lambda: dt.datetime.now(core.KYIV))

    def text(self, template):
        city = core.city_name(self.cfg)
        return template.format(city=city, city_in=self.cfg.get("city_in") or f"у {city}",
                               limit=core.fmt_uah(self.cfg["min_prize_uah"]))

    # ------------------------------------------------------------------ subscribers
    # stored as the "subscribers" document: {"chats": {"<chat id>": {"since": "<iso time>"}}}

    def subscribers(self):
        return list(((self.store.get("subscribers", {}) or {}).get("chats") or {}).keys())

    def subscribe(self, chat_id):
        key, since = str(chat_id), self.clock().isoformat()

        def change(doc):
            doc.setdefault("chats", {}).setdefault(key, {"since": since})
            return doc
        self.store.update("subscribers", change, default={"chats": {}})

    def unsubscribe(self, chat_id):
        key = str(chat_id)

        def change(doc):
            doc.setdefault("chats", {}).pop(key, None)
            return doc
        self.store.update("subscribers", change, default={"chats": {}})

    # ------------------------------------------------------------------ the list

    def check(self, now=None):
        return core.run_check(self.cfg, now or self.clock(), self.store)

    def list_messages(self):
        doc = self.store.get("latest")
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
                self.unsubscribe(member["chat"]["id"])
            return
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat is None:
            return
        command = text.split()[0].lower() if text.startswith("/") else ""
        if "@" in command:  # "/list@some_bot" in a group: only answer when it's addressed to us
            command, addressee = command.split("@", 1)
            username = ((self.store.get("bot", {}) or {}).get("username") or "").lower()
            if username and addressee != username:
                return
        if command == "/start":
            self.subscribe(chat)
            self.send(chat, [self.text(WELCOME)] + self.list_messages())
        elif command == "/list":
            self.send(chat, self.list_messages())
        elif command == "/stop":
            self.unsubscribe(chat)
            self.send(chat, [STOPPED])
        else:
            self.send(chat, [self.text(HELP)])

    def send(self, chat, messages):
        for text in messages:
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
        for chat in chats:
            try:
                self.send(chat, messages)
                summary["delivered"] += 1
            except TelegramError as e:
                if is_gone(e):
                    self.unsubscribe(chat)
                    summary["removed"] += 1
                else:
                    summary["failed"] += 1
                    log.warning("couldn't message a subscriber: %s", e)
            time.sleep(0.05)  # well under Telegram's ~30 messages a second
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
