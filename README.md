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

## Setup

1. Create a Python virtual environment.
2. Install dependencies:
   `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and add the Telegram, Deriv, and Supabase values.
4. For the current Deriv OAuth flow, the App ID is used as the OAuth `client_id` value. The project does not require a separate Deriv client secret for the PKCE authorization-code flow described in Deriv's current OAuth documentation.
5. Start the FastAPI backend:
   `uvicorn app.main:app --host 0.0.0.0 --port 8000`
6. Start the Telegram bot with your webhook or polling setup.

## Security note

- Never commit `.env`.
- OAuth state, PKCE verifier/challenge, and token handling belong in the backend service layer.
- Do not store tokens in Telegram callback data or browser storage.
- Keep `SUPABASE_SERVICE_ROLE_KEY` only in the trusted backend environment; never expose it to Telegram clients or commit it.
- Trade-history schema changes are managed in `supabase/migrations/` and applied to the linked project with `supabase db push --linked`.

## Roadmap

- Milestone 1: Telegram + Deriv public market data
- Milestone 2: Deriv OAuth and `/connect`, `/status`, `/logout`
- Milestone 3: Authenticated account status and portfolio access
- Milestone 4: Demo trading with proposal/buy/contract monitoring
- Milestone 5: Scanner + trading intelligence integration
