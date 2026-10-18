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
- Auto Trade session starts are recorded in Supabase and limited to 3 per user before a 15-minute cooldown, enforced across bot workers. The cooldown begins when the third session starts. When the limit is reached, that user's Auto Trade button displays the remaining cooldown and explains the pause; the limit is atomically checked again when a session is confirmed.
- Consecutive Auto Trade sessions for a user must use different markets. The previous session's market is excluded from the next scan, and Supabase atomically rejects a stale confirmation that would reuse it.
- Users can claim a one-time gift of 200 Yields Tokens from the Telegram welcome flow. The claim is recorded on their persistent Supabase profile, including for users who received the former 150-token OAuth grant.
- The dashboard's Get Tokens button and `/tokens` command show Whop packages for 50, 100, 200, and 500 tokens. The package selection screen links to each checkout and returns to the `/start` menu.
- `/referrals` shows a user's unique invite link and referral earnings. Verified purchases credit the buyer's tokens and pay the referrer 5% of the USD amount paid.
- Referral earnings can be withdrawn at a $10 minimum. Requests are scheduled for the next 15th or month-end payout date in UTC; an admin manually pays them and records the result in the bot.

## Auto Trade market analysis

Auto Trade scans live Deriv market data for the five configured Step Indices.
The indicator scanner ranks available Rise/Fall setups and continues into the
existing stake selection, confirmation, and trade flow when it finds a
directional result. No screenshot or external AI provider is involved. Indicator
signals are not a guarantee or financial advice.

The separate `/scan` command continues to analyze recent real ticks and
one-minute candles from Deriv's public `ticks_history` WebSocket endpoint. It
uses the ticks for 5-second EMA, slope, momentum, ATR, and RSI, and the candles
for 1-minute EMA context. It does not synthesize or fall back to generated
prices. Markets are excluded when their history is too short or the latest
tick is stale.

The scanner computes a weighted directional score from EMA alignment, EMA
slope, 3-bar momentum, price relative to the short- and higher-timeframe EMAs,
and RSI. The weighted score selects bullish, bearish, or neutral direction;
ATR and momentum are reported as context rather than hard eligibility gates.
Markets with insufficient or stale history remain excluded. This scoring
approach does not guarantee a profitable five-tick contract and has not been
validated as profitable by a historical backtest. Live data errors stop a
market scan instead of producing a signal from sample data. Public market data
uses Deriv's unauthenticated public WebSocket. Deriv's documented WebSocket
request budgets and REST limits are
enforced centrally. Connection handshakes also have a separate four-second
minimum interval with jitter as a defensive measure; this is not a published
Deriv handshake limit. A Deriv HTTP 429 pauses outbound calls globally and uses
bounded exponential retries with `Retry-After` support for WebSocket
handshakes. Repeated rate limits stop safely rather than continuing without a
verified Deriv connection. This in-memory limiter covers one bot process; if
deploying multiple workers or replicas, use a shared distributed limiter before
increasing traffic. Deriv may still impose additional or changing limits.
The `/scan` command requests tick and candle history over one public WebSocket
connection for the configured markets. The Auto Trade stake buttons use 0.1%,
0.5%, or 1% of the
selected account balance, calculated to the nearest cent. Users can also
choose Custom amount and enter a stake directly; the amount must be at least
$0.35, no more than 1% of the selected account balance, and have no more than
two decimal places. The selected market and direction remain pinned from
analysis through confirmation. Auto Trade currently has no consecutive-loss stop: Smart Scaling
can increase the next stake substantially after repeated losses. Contracts
expire after five ticks, and a session ends after its target of 3 winning cycles,
token depletion, insufficient balance, or an execution failure. After a
contract is bought, settlement monitoring reconnects with a fresh account URL
and retries transient errors until Deriv confirms the result; no next contract
is purchased before that confirmation. This recovery depends on the bot process
remaining running.

## Deriv rate limiting

Deriv's [limits guide](https://developers.deriv.com/docs/limits/) describes
shared WebSocket request budgets and REST limits, and its
[best-practices guide](https://developers.deriv.com/docs/best-practices/)
recommends reusing connections, pacing bursts, and backing off after rate-limit
responses. The bot serializes outbound WebSocket connection starts and
REST requests through process-wide rolling request budgets:

| WebSocket request group | Per minute | Per hour |
| --- | ---: | ---: |
| `proposal`, `proposal_open_contract`, `buy`, `sell` (shared) | 360 | 14,400 |
| `balance`, `statement` (shared) | 100 | 2,000 |
| `portfolio`, `profit_table` (shared) | 30 | 1,500 |
| All other calls | 220 | 14,400 |

REST calls are also held to 300 per minute and 1,000 per 10 minutes per bot
server IP, plus 80 per minute per logged-in Deriv account. WebSocket `ping`
frames are outside those request budgets. New WebSocket handshakes are spaced
at least four seconds apart as an additional defensive throttle; Deriv's
published request-budget table does not specify a numeric handshake quota.
A 429 pauses all outbound Deriv requests for the provider's `Retry-After`
period when supplied, otherwise for increasing backoff delays.

The limiter is intentionally not a claim that a 429 can never occur: Deriv can
change limits or apply additional shared limits, and an in-memory limiter
cannot coordinate multiple bot processes. Run one bot process, or put a shared
distributed limiter in front of Deriv traffic before scaling to multiple
workers or replicas. Keep the handshake throttle conservative unless Deriv
confirms the applicable connection limit.

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
