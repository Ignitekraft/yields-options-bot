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

## Auto Trade market analysis

Auto Trade reads up to 5,000 real ticks per configured Step Index from Deriv's
public `ticks_history` WebSocket endpoint. The scanner aggregates those ticks
into 5-second bars and 1-minute context bars; it does not synthesize or fall
back to generated prices. A scan is rejected when its history is too short,
the latest tick is stale, or the current volatility is outside its adaptive
range.

A direction is eligible only when the 5-second EMA(8/21), EMA(21) slope, and
3-bar momentum agree with price and the 1-minute EMA(20/50) trend. ATR(14) on
5-second bars is used as a relative volatility filter, and RSI(7) vetoes
stretched entries at 20/80. These conditions are entry filters, not a
guarantee or probability of a profitable 30-second contract. The public market
WebSocket uses the configured Deriv App ID when one is set; live data errors
stop the scan instead of producing a signal from sample data.

## Setup

1. Create a Python virtual environment.
2. Install dependencies:
   `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and add the Telegram and Deriv values. Set `SUPABASE_URL` and the backend-only `SUPABASE_SERVICE_ROLE_KEY` for trade history, Auto Trade, and encrypted OAuth credential storage.
4. Register a Deriv App ID for OAuth. Public market data connects to `wss://api.derivws.com/trading/v1/options/ws/public` without sending the App ID. The project does not require a separate Deriv client secret for the PKCE authorization-code flow.
5. Generate a Fernet key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and set it as the backend-only `DERIV_TOKEN_ENCRYPTION_KEY` environment variable. Keep the same key across deploys and do not rotate it unless stored credentials are re-encrypted.
6. Apply the Supabase migrations with `supabase db push --linked`.
7. Start the FastAPI backend:
   `uvicorn app.main:app --host 0.0.0.0 --port 8000`
8. Start the Telegram bot with your webhook or polling setup.

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
