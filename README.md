# padel-prize-bot

A Telegram bot that watches padel tournaments in Kyiv on [PADO](https://pado.app/uk/ukraine/kyiv/tournaments) and lists the ones whose prize is worth 5 000 ₴ or more: rackets, certificates, cash. Subscribers get a message when a new one appears.

**Live:** [@pado_prize_radar_bot](https://t.me/pado_prize_radar_bot). The bot speaks Ukrainian.

> Not affiliated with PADO. The bot reads the same public tournament data the pado.app website shows, with one request a day.

## Using the bot

Press **Start** in the bot. You get the current list and are subscribed to alerts.

| Command | What it does |
|---|---|
| `/list` | Shows the current list, any time |
| `/stop` | Turns alerts off |
| `/start` | Turns alerts back on and shows the list |

Once a day, around 10:00 Kyiv time, the bot checks PADO. It sends a message only when a tournament newly reaches the threshold (just published, or its "Цінний приз" was named) or when a listed prize changes. Quiet days send nothing.

## How a check works

1. **Tournaments:** one request to PADO's API (`api.pado.app/v2/tournament`) returns every Kyiv tournament for the next 90 days, with titles and descriptions.
2. **Prizes** are read from the title and description:
   - certificate or cash amounts ("сертифікат номіналом 7000 ₴"); entry fees such as "Вартість 1300 грн" are ignored
   - racket brands and models ("🥇Ракетка NOX X-HERO")
   - teasers such as "Цінний приз"
3. **Rackets** are priced at the Ukrainian shops [padel-tennis.com.ua](https://padel-tennis.com.ua) and [tennisgo.com.ua](https://tennisgo.com.ua) at their regular price, cached for 7 days. Prices are approximate on purpose:
   - **від 9 999 ₴:** the exact version isn't named, so this is the cheapest matching racket.
   - **≈ 9 530 ₴:** priced from another model year.
4. **Comparison:** the result is compared with the previous check, to tell what's new or changed.

## Project layout

| File | What it is |
|---|---|
| `pado_prizes.py` | Core: PADO fetch, prize reading, racket prices, comparison with the previous check, report rendering; also a command-line tool. Standard library only. |
| `bot.py` | Bot logic: /start, /list, /stop, the daily broadcast, one-off setup. Texts friends see are at the top. |
| `blob_store.py` | Small JSON documents in a private Vercel Blob store, standard library only |
| `app.py` | The Flask app Vercel runs: the Telegram webhook, the daily check, setup |
| `config.json` | City (as PADO's API spells it, plus `city_name` and `city_in` for texts), threshold (`min_prize_uah`), days ahead, price cache length, `show_maybe` |
| `vercel.json` | Flask preset, Frankfurt region, 60-second limit, the daily cron |
| `requirements.txt` | Flask, the only dependency |
| `tests/` | Offline tests built from real tournament texts, plus bot, storage and app tests |

## Running it locally

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
python3 pado_prizes.py
```

The third line runs the offline tests. The last prints the current list and saves it to `out/` (Markdown, HTML, JSON), with its caches in `data/`. It never messages Telegram, and it needs only the Python standard library (3.9+). Flask is needed only for the web app and its tests.

## Deploying your own

You need a bot token from [@BotFather](https://t.me/BotFather), a Vercel account, and Node.js (for `npx vercel`). Run the steps from the project folder:

1. **Log in, create a project and link this folder to it:**
   ```bash
   npx vercel login
   npx vercel project create my-padel-bot
   npx vercel link --yes --project my-padel-bot
   ```
2. **Create private storage and connect it to the project.** This sets `BLOB_READ_WRITE_TOKEN`:
   ```bash
   npx vercel blob create-store my-padel-bot-data --access private --region fra1 --yes --environment production
   ```
3. **Add the secrets.** The first command asks you to paste the bot token:
   ```bash
   npx vercel env add TELEGRAM_BOT_TOKEN --environment production --sensitive
   python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" > .cron_secret
   npx vercel env add CRON_SECRET --environment production --sensitive --value "$(cat .cron_secret)" --yes
   ```
4. **Deploy:** `npx vercel deploy --prod --yes`
5. **Run the one-time setup** once the deployment is live. It points Telegram at the webhook, sets the command menu and description, and does the first check:
   ```bash
   curl -X POST -H "Authorization: Bearer $(cat .cron_secret)" https://my-padel-bot.vercel.app/api/setup
   ```

The daily check is the cron in `vercel.json` (`0 8 * * *` UTC, which is 10:00 Kyiv time in winter and 11:00 in summer). On Vercel's free plan, scheduled jobs run once a day, at some point within the chosen hour.

## Operating it

The examples use the live bot's address; replace it with yours.

- **Deploy a change:** run the tests, then `npx vercel deploy --prod --yes`.
- **After changing the bot's commands or texts** (in `bot.py`), deploy, then run setup again (step 5 above).
- **After changing how prizes are labelled** (texts in `pado_prizes.py`), refresh quietly once after deploying. Otherwise every relabelled prize looks "changed" and every subscriber gets an alert:
  `curl -H "Authorization: Bearer $(cat .cron_secret)" "https://padel-prize-bot.vercel.app/api/daily?quiet=1"`
- **Run the daily check now, with alerts:** `curl -H "Authorization: Bearer $(cat .cron_secret)" https://padel-prize-bot.vercel.app/api/daily`
- **Read the logs:** `npx vercel logs --since 1h`

## How it's built

- **`app.py` has three routes:**
  - `POST /api/telegram`: Telegram's webhook. It accepts only requests carrying the secret Telegram sends, which is derived from the bot token.
  - `GET /api/daily`: the daily check, protected by `CRON_SECRET`. Vercel Cron sends it automatically.
  - `POST /api/setup`: the one-off setup, also protected by `CRON_SECRET`.
- **Storage:** a private Vercel Blob store holds the subscribers' chat IDs, the latest list, the previous check's results, and the racket-price and club-name caches, all as `pado-bot/*.json`. Writes use conditional updates, so two people pressing Start at the same moment can't overwrite each other.
- **Environment variables** (Production): `TELEGRAM_BOT_TOKEN`, `CRON_SECRET`, `BLOB_READ_WRITE_TOKEN`.

## Caveats

- **PADO's API isn't documented.** It's the one pado.app itself uses, so it can change. If it does, the daily check fails with an error in the logs and nobody gets a wrong list.
- **Prize text is written by organisers in many styles.** Unusual wording can be missed or misread, which is why the bot tells people to check the tournament page before signing up.
- **Shop prices are a rough guide** to a prize's value, not a quote.
