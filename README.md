# Yields

Yields is a Telegram-first trading intelligence assistant built around the official Deriv API.

## Mission

The product is designed so Telegram is the user's interface, Yields is the intelligence layer, and Deriv provides the market-data and execution infrastructure.

## Current milestone

This repository contains the first milestone: Telegram + Deriv public market data.

Implemented:
- `/start` messaging
- `/scan` command flow via the scanner service
- Deriv public market-data service with a public websocket endpoint
- normalized tick handling
- modular service architecture for future OAuth and trading work

## Architecture

- `app/bot` contains Telegram-facing interaction code.
- `app/services` owns business logic, market scanning, and auth orchestration.
- `app/services/deriv` encapsulates Deriv public data access.
- `app/main.py` exposes the HTTPS health and OAuth callback routes.
- Supabase stores Telegram user profiles, read-only Deriv balance snapshots, per-contract trade history, and end-of-session summaries.
- `/stats` reports the last 10 finalized Auto Trade sessions; session win rate is based on positive session PnL, while direction accuracy is calculated from settled contract ticks.
- One Yields Token is reserved for each contract purchase. Explicitly rejected purchases are refunded; an uncertain purchase outcome is not refunded to prevent an uncharged possible trade.
- Users can claim a one-time gift of 200 Yields Tokens from the Telegram welcome flow. The claim is recorded on their persistent Supabase profile, including for users who received the former 150-token OAuth grant.
- The dashboard's Get Tokens button and `/tokens` command show Whop packages for 50, 100, 200, and 500 tokens. The package selection screen links to each checkout and returns to the `/start` menu.
- `/referrals` shows a user's unique invite link and referral earnings. Verified purchases credit the buyer's tokens and pay the referrer 5% of the USD amount paid.
- Referral earnings can be withdrawn at a $10 minimum. Requests are scheduled for the next 15th or month-end payout date in UTC; an admin manually pays them and records the result in the bot.

## Auto Trade market analysis

Auto Trade reads recent real ticks and 60 one-minute candles per configured
Step Index from Deriv's public `ticks_history` WebSocket endpoint. The scanner
uses the ticks for 5-second EMA, slope, momentum, ATR, and RSI, and the candles
for 1-minute EMA context. It does not synthesize or fall back to generated
prices. A market is excluded when its history is too short or its latest tick
is stale.

The scanner computes a weighted directional score from EMA alignment, EMA
slope, 3-bar momentum, price relative to the short- and higher-timeframe EMAs,
and RSI. It presents the available market with the strongest non-neutral
directional score, rather than requiring every indicator to agree. ATR and
momentum are included as context instead of hard vetoes. The score is an
indicator balance, not a probability or guarantee of a profitable 30-second
contract. Live data errors stop a market scan instead of producing a signal
from sample data. Public market data uses Deriv's unauthenticated public
WebSocket; HTTP 429 handshake responses receive bounded retries and honor
Deriv's `Retry-After` header. Repeated rate limits stop safely rather than
continuing without a verified Deriv connection. The Auto Trade product flow
still requires the user to connect an account before scanning or trading.
The scan requests tick and candle history over one public WebSocket connection
for the configured markets. The stake buttons use 0.1%, 0.5%, or 1% of the
selected account balance, calculated to the nearest cent. Users can also
choose Custom amount and enter a stake directly; the amount must be positive,
have no more than two decimal places, and not exceed the selected account
balance. The selected market and direction remain pinned from analysis through
confirmation.

## Setup

1. Create a Python virtual environment.
2. Install dependencies:
   `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and add the Telegram and Deriv values. Set `TELEGRAM_BOT_USERNAME`, `SUPABASE_URL`, and the backend-only `SUPABASE_SERVICE_ROLE_KEY` for user profiles, referrals, Auto Trade, and encrypted credential storage. Configure `WHOP_WEBHOOK_SECRET` to enable payment webhooks; `WHOP_TELEGRAM_USER_METADATA_KEY` and `WHOP_TOKEN_AMOUNT_METADATA_KEY` default to `telegram_user_id` and `token_amount`.
4. Register a Deriv App ID for OAuth. Public market data connects to `wss://api.derivws.com/trading/v1/options/ws/public` without sending the App ID. The project does not require a separate Deriv client secret for the PKCE authorization-code flow.
5. Generate a Fernet key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and set it as the backend-only `DERIV_TOKEN_ENCRYPTION_KEY` environment variable. Keep the same key across deploys and do not rotate it unless stored credentials are re-encrypted.
6. Apply the Supabase migrations with `supabase db push --linked`.
7. Start the FastAPI backend:
   `uvicorn app.main:app --host 0.0.0.0 --port 8000`
8. Start the Telegram bot with your webhook or polling setup.

## Welcome gifts, referrals, and payouts

Apply the new migration with the other Supabase migrations before deploying this
feature. The 200-token welcome gift is explicitly claimed through Telegram; the
old 150-token OAuth grant is disabled. Existing users, including recipients of
the former 150-token grant, can claim the new gift once.

For Whop webhook crediting, configure a `payment.succeeded` webhook to
`https://<your-backend-host>/webhooks/whop` and set `WHOP_WEBHOOK_SECRET`. Every
checkout must include the Telegram user ID and token quantity in payment
metadata, using the keys configured by `WHOP_TELEGRAM_USER_METADATA_KEY` and
`WHOP_TOKEN_AMOUNT_METADATA_KEY`. The webhook currently accepts USD purchases.
Existing static checkout links must be configured to supply this metadata;
otherwise, verify a purchase with `/admin_verify_purchase` after confirming it
in Whop. Both paths deduplicate by purchase ID.

Promote trusted bot operators to admins after their Telegram profile exists by
running this SQL in the Supabase SQL editor:

```sql
update public.users
set is_admin = true
where telegram_user_id = '<trusted-telegram-user-id>';
```

Admins can manually credit a purchase with
`/admin_verify_purchase <buyer_telegram_id> <whop_payment_id> <token_amount> <amount_usd> [currency]`.
Users manage their invite and payout details with `/referrals`, `/payout`, and
`/payout_details`. Admins see due payout requests with `/admin_withdrawals`,
then record manual transfers with `/admin_mark_paid <withdrawal_id>` or
`/admin_mark_failed <withdrawal_id> <reason>`. Failure returns the earnings to
the user's available balance and notifies them with the recorded reason.

Payout details are encrypted using `DERIV_TOKEN_ENCRYPTION_KEY`; keep that key
stable across deployments. Admin processing and payout notifications require
the bot to be running with the configured Telegram token.

## Security note

- Never commit `.env`.
- OAuth state, PKCE verifier/challenge, and token handling belong in the backend service layer.
- Do not store tokens in Telegram callback data or browser storage.
- Keep `SUPABASE_SERVICE_ROLE_KEY` only in the trusted backend environment; never expose it to Telegram clients or commit it.
- Deriv access and refresh tokens are encrypted before storage in `deriv_oauth_credentials`, a table accessible only through the backend service role. Keep `DERIV_TOKEN_ENCRYPTION_KEY` private and stable; loss or rotation of this key requires users to reconnect unless credentials are re-encrypted first.
- Trade-history schema changes are managed in `supabase/migrations/` and applied to the linked project with `supabase db push --linked`.

## Roadmap

- Milestone 1: Telegram + Deriv public market data
- Milestone 2: Deriv OAuth and `/connect`, `/status`, `/logout`
- Milestone 3: Authenticated account status and portfolio access
- Milestone 4: Demo trading with proposal/buy/contract monitoring
- Milestone 5: Scanner + trading intelligence integration
