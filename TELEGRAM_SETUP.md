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

Each digest is built per user: the signal-type toggles and **Watchlist only** are both applied before sending, so two users rarely get the same message. Watchlists live in the browser, so the app publishes just the symbol list with the user's alert prefs for the scanner to filter on. A user whose client has never published one keeps receiving unfiltered alerts rather than being silently muted.

One Telegram chat maps to exactly one account. Linking a chat that already belongs to another account moves it, so the previous account stops receiving alerts instead of both getting them.

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
- Each user gets **one digest per scan** (up to 10 lines, rest in the app), not 10 separate DMs.
- Sends are paced at **25/sec** (Telegram cap ~30/sec). 1,000 users ≈ 40 seconds, off the scan loop.
- Scans run every 60s, so a user receives **at most one DM per minute** — both detectors (squeeze and signals) are merged into that single digest. Quiet scans send nothing, since alerts only fire on a state transition.
- Throughput headroom at one digest per user per scan: 100 users ≈ 1.7/sec, 500 ≈ 8/sec, 1,000 ≈ 17/sec. The ~30/sec ceiling is reached around 1,800 users.
- 429 (too many requests) waits `retry_after` and retries a few times; leftover DMs stay queued.
- Do not run a second `getUpdates` poller (only one process can poll). Keep it on live_scan.

## Troubleshooting: Account says “Not linked” after Start

Check in this order — each step rules out one cause.

1. **A webhook owns the bot.** Open `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`. Anything other than `"url":""` means Telegram delivers updates there and never to the scanner, so `getUpdates` returns 409 forever. Clear it with `/deleteWebhook`. Third-party bot services (Livegram and similar) set this silently when you connect a bot to them.
2. **The scanner is not polling.** Run `select * from public.app_settings where key = 'telegram_bot_username';`. The scanner writes that row from inside the polling routine, so a missing row means `TELEGRAM_BOT_TOKEN` is unset on the live-scan service, or the deploy is stale.
3. **Wrong Supabase key.** `SUPABASE_SERVICE_KEY` must be the service-role key. With the anon key, RLS hides `user_telegram` from the link lookup and the scanner logs "could not read user_telegram".
4. **Expired code.** Codes last 15 minutes. Generate a fresh one from Account.

If a bot token ever sat in a third-party service, treat it as compromised: `/revoke` in BotFather, update `TELEGRAM_BOT_TOKEN`, redeploy. Whoever holds the token can read every message to the bot and message every user who started it.
