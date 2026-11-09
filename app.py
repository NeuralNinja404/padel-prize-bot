"""Vercel entrypoint for the PADO prize bot.

  POST /api/telegram  Telegram webhook (checked against the secret Telegram sends with each update)
  GET  /api/daily     the daily check, called by Vercel Cron with CRON_SECRET
                      (?quiet=1 refreshes the stored list without messaging anyone)
  POST /api/setup     one-off: point Telegram at the webhook, set the command menu, do the first check
                      (also needs CRON_SECRET)

Environment: TELEGRAM_BOT_TOKEN, CRON_SECRET, and the Blob store's variables (see blob_store.py).
"""
from __future__ import annotations

import hmac
import logging
import os

from flask import Flask, jsonify, request

import bot as botlib
import pado_prizes as core
from blob_store import BlobStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pado_app")
app = Flask(__name__)


def make_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return botlib.Bot(botlib.Telegram(token), BlobStore.from_env(request.headers), core.load_config(),
                      admin_code=botlib.admin_code(os.environ.get("CRON_SECRET", "")))


def has_cron_secret():
    secret = os.environ.get("CRON_SECRET", "")
    return bool(secret) and hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {secret}")


@app.get("/")
def index():
    return "PADO prize bot is running."


@app.post("/api/telegram")
def telegram_webhook():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
                                            botlib.webhook_secret(token)):
        return "forbidden", 403
    try:
        make_bot().handle_update(request.get_json(silent=True) or {})
    except Exception:
        log.exception("handling a Telegram update failed")
    return "ok"  # always 200, or Telegram keeps re-sending the same update


@app.get("/api/daily")
def daily():
    if not has_cron_secret():
        return "unauthorized", 401
    summary = make_bot().daily(broadcast=request.args.get("quiet") != "1")
    log.info("daily check: %s", summary)
    return jsonify(summary)


@app.post("/api/setup")
def setup():
    if not has_cron_secret():
        return "unauthorized", 401
    host = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or request.host
    result = make_bot().setup(f"https://{host}")
    log.info("setup: %s", result)
    return jsonify(result)
