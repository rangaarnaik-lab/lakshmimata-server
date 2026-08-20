# Telegram alerts — operator setup

Personal 1:1 messages from **your bot**, filtered by each user’s 🔔 alert prefs (HY, HT, Squeeze, Stage 2, …). Telegram is free. ~1,000 users will not slow Vercel — the scanner sends the messages.

## 1. Create the bot (2 minutes)

1. Open Telegram, search **@BotFather**.
2. Send `/newbot`.
3. Pick a name (e.g. `Lakshmimata Alerts`) and a username ending in `bot` (e.g. `LakshmimataAlertsBot`).
4. Copy the **token** (`123456:ABC…`).
5. Optional: `/setprivacy` → **Disable** so the bot can read `/start` in groups (not required for DMs).
6. Send `/start` to **your** bot once so it can talk to you.

Your own chat id (for the EOD digest only):

- Message [@userinfobot](https://t.me/userinfobot), or
- Open `https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging the bot and copy `message.chat.id`.

## 2. Railway / scanner env

On the **live scan** service (`angelic-strength` / `SERVICE_MODE` not fundamentals):

| Variable | Required | What it is |
|----------|----------|------------|
| `TELEGRAM_BOT_TOKEN` | Yes | BotFather token |
| `TELEGRAM_BOT_USERNAME` | Yes | Username **without** `@` (e.g. `LakshmimataAlertsBot`) |
| `TELEGRAM_CHAT_ID` | Optional | Your personal chat id for the **EOD digest** only |

Redeploy the scanner after saving.

Optional on Vercel (fallback if `app_settings` is empty):

`VITE_TELEGRAM_BOT_USERNAME=LakshmimataAlertsBot`

## 3. Database

Run `add_user_telegram.sql` in the Supabase SQL editor (same as Squeeze Pro).

The scanner writes `app_settings.telegram_bot_username` on boot so the Account page can show **Connect Telegram** without a frontend rebuild.

## 4. Connect as a user

Account → **Telegram alerts** → **Connect Telegram** → Start in the app → wait for “Connected @username”.

Prefs already chosen under the header 🔔 apply to Telegram (same HY / Squeeze / watchlist-only flags). Toggle **Send these alerts to Telegram** on that card.

## 5. Message labels

Example:

```
🌀 BB Squeeze ↑ Long
RELIANCE   RS 87   +1.4%
IT · ₹2,940
Research alert — not advice
```

## Limits

- Users **must** tap Start. You cannot message a Telegram account that never opened the bot.
- ~30 messages/second. 1,000 filtered alerts is fine.
- Do not run a second `getUpdates` poller (only one process can poll). Keep it on live_scan.
