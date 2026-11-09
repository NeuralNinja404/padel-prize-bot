"""Offline tests for the Telegram bot (bot.py), its private Blob storage (blob_store.py) and the web app (app.py).

Run:  .venv/bin/python -m unittest discover -s tests -v      (the app tests need Flask)
"""
import datetime as dt
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import blob_store  # noqa: E402
import bot as botlib  # noqa: E402
import pado_prizes as core  # noqa: E402

NOW = dt.datetime(2026, 11, 10, 10, 5, tzinfo=core.KYIV)
TOKEN = "123456789:" + "A" * 35


class MemoryStore:
    """Same get/put/update interface as FileStore and BlobStore; copies values like a real store would."""

    def __init__(self):
        self.docs = {}

    def get(self, key, default=None):
        return json.loads(json.dumps(self.docs[key])) if key in self.docs else default

    def put(self, key, value):
        self.docs[key] = json.loads(json.dumps(value))

    def update(self, key, change, default=None):
        value = change(self.get(key, json.loads(json.dumps(default))))
        self.put(key, value)
        return value


class FakeTelegram:
    token = TOKEN

    def __init__(self, failures=None):
        self.sent, self.calls, self.failures = [], [], failures or {}

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"username": "padel_prize_radar_bot"} if method == "getMe" else True

    def send(self, chat_id, text):
        if str(chat_id) in self.failures:
            raise self.failures[str(chat_id)]
        self.sent.append((str(chat_id), text))


def entry(i, status="yes", new=False):
    return {"id": i, "status": status, "new": new, "changed": False, "when": "Sat 14 Nov 16:00–18:00",
            "club": "P12 Padel Club", "url": f"https://pado.app/uk/ukraine/kyiv/tournaments/t-{i}",
            "name": f"Padel Marathon D/D+ #{i}", "prize": "Bullpadel NEURON 02 CMF 27",
            "worth": "15 400 ₴" if status == "yes" else "", "fee": 1300, "players": 11, "max_players": 24,
            "full": False, "waitlist": 0, "pairs": False, "who": "",
            "price": {"url": "https://padel-tennis.com.ua/x"} if status == "yes" else None}


def make_bot(store=None, telegram=None):
    store = store or MemoryStore()
    store.put("latest", store.get("latest") or {"updated": NOW.isoformat(), "checked": 113,
                                                "tournaments": [entry(1), entry(2, "maybe")]})
    return botlib.Bot(telegram or FakeTelegram(), store, core.DEFAULT_CONFIG, clock=lambda: NOW)


def message(bot, chat, text):
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": chat}, "text": text}})


class Commands(unittest.TestCase):
    def test_start_subscribes_and_sends_the_welcome_and_list(self):
        bot = make_bot()
        message(bot, 42, "/start")
        self.assertEqual(bot.subscribers(), ["42"])
        texts = [text for chat, text in bot.tg.sent]
        self.assertIn("Я стежу за турнірами з паделу у Києві на PADO", texts[0])
        self.assertIn("1 турнір · перевірено Вт, 10 листопада, 10:05", texts[1])
        self.assertIn("t-1", "\n".join(texts))

    def test_start_twice_keeps_one_subscription(self):
        bot = make_bot()
        message(bot, 42, "/start")
        message(bot, 42, "/start")
        self.assertEqual(bot.subscribers(), ["42"])

    def test_stop_unsubscribes(self):
        bot = make_bot()
        message(bot, 42, "/start")
        message(bot, 7, "/start")
        message(bot, 42, "/stop")
        self.assertEqual(bot.subscribers(), ["7"])
        self.assertIn("Сповіщення вимкнено", bot.tg.sent[-1][1])

    def test_list_answers_without_subscribing(self):
        bot = make_bot()
        message(bot, 42, "/list")
        self.assertEqual(bot.subscribers(), [])
        self.assertIn("t-1", bot.tg.sent[0][1])

    def test_anything_else_gets_the_help(self):
        bot = make_bot()
        message(bot, 42, "hello")
        self.assertTrue(bot.tg.sent[0][1].startswith("/list"))

    def test_commands_for_another_bot_are_ignored(self):
        bot = make_bot()
        bot.store.put("bot", {"username": "padel_prize_radar_bot"})
        message(bot, -100, "/list@some_other_bot")
        message(bot, -100, "/list@Padel_Prize_Radar_Bot")
        self.assertEqual(len(bot.tg.sent), 1)

    def test_blocking_the_bot_unsubscribes(self):
        bot = make_bot()
        message(bot, 42, "/start")
        bot.handle_update({"my_chat_member": {"chat": {"id": 42}, "new_chat_member": {"status": "kicked"}}})
        self.assertEqual(bot.subscribers(), [])

    def test_rate_limit_waits_and_retries_once(self):
        tg = FakeTelegram()
        calls = []

        def flaky_send(chat, text):
            calls.append(chat)
            if len(calls) == 1:
                raise botlib.TelegramError(429, "Too Many Requests", retry_after=1)
            tg.sent.append((str(chat), text))
        tg.send = flaky_send
        bot = make_bot(telegram=tg)
        with mock.patch.object(botlib.time, "sleep") as sleep:
            message(bot, 42, "/stop")
        sleep.assert_called_once_with(1)
        self.assertEqual(len(tg.sent), 1)


class Daily(unittest.TestCase):
    def run_daily(self, entries, bot):
        with mock.patch.object(core, "run_check", lambda cfg, now, store: (entries, 113)), \
                mock.patch.object(botlib.time, "sleep"):
            return bot.daily()

    def test_news_goes_to_every_subscriber(self):
        bot = make_bot()
        for chat in (1, 2):
            message(bot, chat, "/start")
        bot.tg.sent.clear()
        summary = self.run_daily([entry(10, new=True), entry(11)], bot)
        self.assertEqual(sorted(chat for chat, _ in bot.tg.sent), ["1", "2"])
        self.assertIn("t-10", bot.tg.sent[0][1])
        self.assertNotIn("t-11", bot.tg.sent[0][1])
        self.assertEqual((summary["news"], summary["delivered"], summary["subscribers"]), (1, 2, 2))

    def test_a_quiet_day_sends_nothing(self):
        bot = make_bot()
        message(bot, 1, "/start")
        bot.tg.sent.clear()
        summary = self.run_daily([entry(10), entry(11, "maybe")], bot)
        self.assertEqual((bot.tg.sent, summary["news"]), ([], 0))

    def test_a_quiet_refresh_sends_nothing_even_with_news(self):
        bot = make_bot()
        message(bot, 1, "/start")
        bot.tg.sent.clear()
        with mock.patch.object(core, "run_check", lambda cfg, now, store: ([entry(10, new=True)], 113)):
            summary = bot.daily(broadcast=False)
        self.assertEqual((bot.tg.sent, summary["news"], summary["quiet"]), ([], 1, True))

    def test_people_who_blocked_the_bot_are_removed_but_others_are_kept(self):
        failures = {"1": botlib.TelegramError(403, "Forbidden: bot was blocked by the user"),
                    "2": botlib.TelegramError(400, "Bad Request: can't parse entities")}
        bot = make_bot(telegram=FakeTelegram(failures))
        bot.store.put("subscribers", {"chats": {"1": {}, "2": {}, "3": {}}})
        summary = self.run_daily([entry(10, new=True)], bot)
        self.assertEqual(sorted(bot.subscribers()), ["2", "3"])
        self.assertEqual((summary["delivered"], summary["removed"], summary["failed"]), (1, 1, 1))


class Setup(unittest.TestCase):
    def test_setup_points_telegram_at_the_webhook_and_does_the_first_check(self):
        store, tg = MemoryStore(), FakeTelegram()
        bot = botlib.Bot(tg, store, core.DEFAULT_CONFIG, clock=lambda: NOW)

        def first_check(cfg, now, store):
            store.put("latest", {"updated": now.isoformat(), "tournaments": [entry(1)]})
            return [entry(1), entry(2, "maybe")], 113
        with mock.patch.object(core, "run_check", first_check):
            result = bot.setup("https://padel-prize-bot.vercel.app/")
        calls = dict(tg.calls)
        self.assertEqual(calls["setWebhook"]["url"], "https://padel-prize-bot.vercel.app/api/telegram")
        self.assertEqual(calls["setWebhook"]["secret_token"], botlib.webhook_secret(TOKEN))
        self.assertEqual([c["command"] for c in calls["setMyCommands"]["commands"]], ["list", "start", "stop", "help"])
        self.assertEqual(store.get("bot"), {"username": "padel_prize_radar_bot"})
        self.assertEqual((result["checked"], result["listed"]), (113, 1))

    def test_webhook_secret_is_stable_and_allowed_by_telegram(self):
        secret = botlib.webhook_secret(TOKEN)
        self.assertEqual(secret, botlib.webhook_secret(TOKEN))
        self.assertRegex(secret, r"^[A-Za-z0-9_-]{1,256}$")
        self.assertNotIn(TOKEN, secret)


class FakeResponse:
    def __init__(self, body=b"{}", headers=None):
        self.body, self.headers = body, headers or {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(body))


class TelegramClient(unittest.TestCase):
    def test_errors_never_contain_the_token(self):
        def refuse(req, timeout=None):
            raise http_error(req.full_url, 403, b'{"ok":false,"error_code":403,"description":"Forbidden"}')
        with mock.patch.object(botlib.urllib.request, "urlopen", refuse):
            with self.assertRaises(botlib.TelegramError) as caught:
                botlib.Telegram(TOKEN).send(42, "hi")
        self.assertEqual(caught.exception.code, 403)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_messages_are_sent_as_html_without_link_previews(self):
        seen = []

        def accept(req, timeout=None):
            seen.append(json.loads(req.data))
            return FakeResponse(b'{"ok":true,"result":{}}')
        with mock.patch.object(botlib.urllib.request, "urlopen", accept):
            botlib.Telegram(TOKEN).send(42, "<b>hi</b>")
        self.assertEqual((seen[0]["chat_id"], seen[0]["parse_mode"], seen[0]["link_preview_options"]),
                         (42, "HTML", {"is_disabled": True}))


class BlobStorage(unittest.TestCase):
    # a made-up token, assembled so secret scanners don't mistake it for a real one
    RW = "vercel_blob_rw" + "_AbC123storeid_secretpart"

    def test_credentials_from_the_environment(self):
        with mock.patch.dict(os.environ, {"BLOB_READ_WRITE_TOKEN": self.RW}, clear=False):
            store = blob_store.BlobStore.from_env()
        self.assertEqual((store.token, store.store_id), (self.RW, "AbC123storeid"))
        env = {"BLOB_READ_WRITE_TOKEN": "", "BLOB_STORE_ID": "store_XyZ", "VERCEL_OIDC_TOKEN": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            store = blob_store.BlobStore.from_env({"x-vercel-oidc-token": "oidc.jwt"})
        self.assertEqual((store.token, store.store_id, store.extra_headers),
                         ("oidc.jwt", "XyZ", {"x-vercel-blob-store-id": "XyZ"}))

    def test_reads_are_private_and_uncached_and_missing_documents_give_the_default(self):
        seen = []

        def missing(req, timeout=None):
            seen.append(req)
            raise http_error(req.full_url, 404, b"")
        store = blob_store.BlobStore(self.RW, "AbC123storeid")
        with mock.patch.object(blob_store.urllib.request, "urlopen", missing):
            self.assertEqual(store.get("state", {"x": 1}), {"x": 1})
        self.assertEqual(seen[0].full_url,
                         "https://AbC123storeid.private.blob.vercel-storage.com/pado-bot/state.json?cache=0")
        self.assertEqual(seen[0].get_header("Authorization"), f"Bearer {self.RW}")

    def test_writes_are_private_overwrites(self):
        seen = []

        def accept(req, timeout=None):
            seen.append(req)
            return FakeResponse()
        store = blob_store.BlobStore(self.RW, "AbC123storeid")
        with mock.patch.object(blob_store.urllib.request, "urlopen", accept):
            store.put("state", {"ok": True})
        req = seen[0]
        self.assertEqual((req.get_method(), req.full_url),
                         ("PUT", "https://vercel.com/api/blob/?pathname=pado-bot%2Fstate.json"))
        self.assertEqual((req.get_header("X-vercel-blob-access"), req.get_header("X-allow-overwrite")), ("private", "1"))
        self.assertEqual(json.loads(req.data), {"ok": True})

    def test_update_retries_when_someone_else_wrote_first(self):
        etags = iter(['"v1"', '"v2"'])
        writes = []

        def server(req, timeout=None):
            if req.get_method() == "GET":
                return FakeResponse(b'{"chats": {}}', {"etag": next(etags)})
            writes.append(req.get_header("X-if-match"))
            if len(writes) == 1:
                raise http_error(req.full_url, 412, b'{"error":{"code":"precondition_failed"}}')
            return FakeResponse()
        store = blob_store.BlobStore(self.RW, "AbC123storeid")
        with mock.patch.object(blob_store.urllib.request, "urlopen", server), \
                mock.patch.object(blob_store.time, "sleep"):
            result = store.update("subscribers", lambda d: {"chats": dict(d["chats"], **{"42": {}})})
        self.assertEqual(writes, ['"v1"', '"v2"'])
        self.assertEqual(result, {"chats": {"42": {}}})


try:
    import flask  # noqa: F401
    import app as webapp
except ImportError:  # the system Python has no Flask; run these with .venv/bin/python
    webapp = None


@unittest.skipIf(webapp is None, "Flask isn't installed")
class WebApp(unittest.TestCase):
    def setUp(self):
        self.client = webapp.app.test_client()
        self.env = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "CRON_SECRET": "cron-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_webhook_rejects_requests_without_telegrams_secret(self):
        with mock.patch.object(webapp, "make_bot") as make:
            self.assertEqual(self.client.post("/api/telegram", json={}).status_code, 403)
            reply = self.client.post("/api/telegram", json={"update_id": 1},
                                     headers={"X-Telegram-Bot-Api-Secret-Token": botlib.webhook_secret(TOKEN)})
        self.assertEqual(reply.status_code, 200)
        make.return_value.handle_update.assert_called_once_with({"update_id": 1})

    def test_webhook_answers_200_even_when_handling_fails(self):
        with mock.patch.object(webapp, "make_bot", side_effect=RuntimeError("boom")), self.assertLogs("pado_app", "ERROR"):
            reply = self.client.post("/api/telegram", json={},
                                     headers={"X-Telegram-Bot-Api-Secret-Token": botlib.webhook_secret(TOKEN)})
        self.assertEqual(reply.status_code, 200)

    def test_daily_and_setup_need_the_cron_secret(self):
        with mock.patch.object(webapp, "make_bot") as make:
            make.return_value.daily.return_value = {"news": 0}
            self.assertEqual(self.client.get("/api/daily").status_code, 401)
            self.assertEqual(self.client.post("/api/setup").status_code, 401)
            reply = self.client.get("/api/daily", headers={"Authorization": "Bearer cron-secret"})
        self.assertEqual((reply.status_code, reply.get_json()), (200, {"news": 0}))
        make.return_value.daily.assert_called_once_with(broadcast=True)

    def test_daily_can_refresh_quietly(self):
        with mock.patch.object(webapp, "make_bot") as make:
            make.return_value.daily.return_value = {"news": 0}
            self.client.get("/api/daily?quiet=1", headers={"Authorization": "Bearer cron-secret"})
        make.return_value.daily.assert_called_once_with(broadcast=False)


if __name__ == "__main__":
    unittest.main()
