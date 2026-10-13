"""
trading_ws.py
─────────────────────────────────────────────────────────────────────
All Deriv trade execution for Yields Bot.

Protocol: WebSocket only (wss://ws.derivws.com/websockets/v3)
Deriv has NO REST trading API. Every trade goes through:
  authorize → proposal → buy → proposal_open_contract (wait for settlement)

Step Index symbols (verified against Deriv active_symbols endpoint):
  step_100, step_200, step_300, step_400, step_500
  Contract type is always CALL (Rise) or PUT (Fall).
"""

import asyncio
import json
import logging
from typing import Callable, Optional

import websockets

log = logging.getLogger(__name__)

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

# ─────────────────────────────────────────────────────────────────
#  LOW-LEVEL: single trade + wait for settlement
# ─────────────────────────────────────────────────────────────────

async def execute_and_wait(
    token: str,
    symbol: str,
    contract_type: str,   # "CALL" or "PUT"
    stake: float,
    duration: int = 30,   # seconds
) -> dict:
    """
    Opens a WebSocket connection, places one trade, waits for it to settle,
    then returns the closed contract dict.

    Raises on any API error so the caller can handle it cleanly.
    """
    async with websockets.connect(
        DERIV_WS_URL,
        open_timeout=15,
        ping_interval=20,
        ping_timeout=10,
    ) as ws:

        async def rpc(payload: dict, label: str) -> dict:
            """Send a message and return the next meaningful response."""
            await ws.send(json.dumps(payload))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                msg = json.loads(raw)
                # Skip unrelated subscription pings
                if "error" in msg:
                    raise RuntimeError(
                        f"{label} error: {msg['error']['message']} "
                        f"(code: {msg['error'].get('code', '?')})"
                    )
                # Return the first message that carries the expected key
                for key in ("authorize", "proposal", "buy", "proposal_open_contract"):
                    if key in msg:
                        return msg
                # Ignore keep-alive or unrelated messages and wait for next
                log.debug("Skipping unrelated message during %s: %s", label, list(msg.keys()))

        # ── 1. Authorize ──────────────────────────────────────
        auth = await rpc({"authorize": token}, "authorize")
        log.info("Authorised as %s", auth["authorize"].get("loginid"))

        # ── 2. Proposal ───────────────────────────────────────
        # IMPORTANT: field is "symbol", NOT "underlying_symbol"
        proposal_msg = await rpc(
            {
                "proposal":       1,
                "amount":         stake,
                "basis":          "stake",
                "contract_type":  contract_type,
                "currency":       "USD",
                "duration":       duration,
                "duration_unit":  "s",
                "symbol":         symbol,       # ← CORRECT field name
            },
            "proposal",
        )
        proposal_id = proposal_msg["proposal"]["id"]
        ask_price   = proposal_msg["proposal"]["ask_price"]
        log.info("Proposal %s received — ask: %s", proposal_id, ask_price)

        # ── 3. Buy ────────────────────────────────────────────
        buy_msg = await rpc(
            {"buy": proposal_id, "price": ask_price},
            "buy",
        )
        contract_id = buy_msg["buy"]["contract_id"]
        log.info("Contract %s opened", contract_id)

        # ── 4. Subscribe and wait for settlement ─────────────
        await ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id":            contract_id,
            "subscribe":              1,
        }))

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=duration + 30)
            msg = json.loads(raw)

            if "error" in msg:
                raise RuntimeError(
                    f"Contract monitor error: {msg['error']['message']}"
                )

            if "proposal_open_contract" in msg:
                poc = msg["proposal_open_contract"]
                # Contract is settled when is_sold is True
                if poc.get("is_sold"):
                    log.info(
                        "Contract %s settled — P/L: %s",
                        contract_id, poc.get("profit")
                    )
                    return poc   # contains profit, status, payout, etc.

# ─────────────────────────────────────────────────────────────────
#  HIGH-LEVEL: 5-cycle trade sequence with Telegram feedback
# ─────────────────────────────────────────────────────────────────

async def run_trade_cycles(
    token: str,
    symbol: str,
    direction: str,          # "CALL" or "PUT"
    stake: float,
    cycles: int = 5,
    duration: int = 30,      # seconds per contract
    notify: Optional[Callable] = None,   # async fn(str) → sends Telegram message
) -> tuple[list, float]:
    """
    Runs `cycles` consecutive trades on `symbol` in `direction`.
    Waits for each contract to settle before placing the next.
    Calls `notify(message)` after each cycle if provided.

    Returns (results_list, total_profit).

    Usage:
        results, total = await run_trade_cycles(
            token=user_token,
            symbol="step_100",
            direction="CALL",
            stake=10.0,
            cycles=5,
            duration=30,
            notify=lambda msg: context.bot.send_message(chat_id, msg),
        )
    """
    results      = []
    total_profit = 0.0

    symbol_display = symbol.replace("_", " ").upper()
    arrow = "📈" if direction == "CALL" else "📉"

    if notify:
        await notify(
            f"{arrow} *Trade Sequence Started*\n"
            f"Asset: `{symbol_display}`\n"
            f"Direction: `{direction}`\n"
            f"Stake: `${stake:.2f}` × {cycles} cycles"
        )

    for cycle in range(1, cycles + 1):
        if notify:
            await notify(f"🔄 Cycle {cycle}/{cycles} — placing `{direction}` on `{symbol_display}`…")

        try:
            poc = await execute_and_wait(
                token=token,
                symbol=symbol,
                contract_type=direction,
                stake=stake,
                duration=duration,
            )

            profit    = float(poc.get("profit", 0))
            payout    = float(poc.get("payout", 0))
            sell_spot = poc.get("sell_spot", "—")
            won       = profit > 0

            total_profit += profit
            results.append({
                "cycle":   cycle,
                "won":     won,
                "profit":  profit,
                "payout":  payout,
            })

            status_icon = "✅" if won else "❌"
            running_icon = "🟢" if total_profit >= 0 else "🔴"

            if notify:
                await notify(
                    f"{status_icon} *Cycle {cycle}/{cycles} {'WON' if won else 'LOST'}*\n"
                    f"P/L this cycle: `${profit:+.2f}`\n"
                    f"Exit price: `{sell_spot}`\n"
                    f"{running_icon} Running total: `${total_profit:+.2f}`"
                )

        except Exception as exc:
            log.exception("Cycle %d failed: %s", cycle, exc)
            results.append({"cycle": cycle, "error": str(exc)})

            if notify:
                await notify(
                    f"⚠️ *Cycle {cycle}/{cycles} Error*\n"
                    f"`{exc}`\n"
                    f"Skipping to next cycle…"
                )
            # Small back-off before next attempt
            await asyncio.sleep(3)

    # ── Final summary ─────────────────────────────────────────
    wins   = sum(1 for r in results if r.get("won"))
    losses = sum(1 for r in results if not r.get("won") and "error" not in r)
    errors = sum(1 for r in results if "error" in r)

    outcome_icon = "🟢 Profit" if total_profit > 0 else ("🔴 Loss" if total_profit < 0 else "⚪ Break-even")

    if notify:
        await notify(
            f"📊 *Sequence Complete*\n"
            f"Wins: `{wins}` | Losses: `{losses}` | Errors: `{errors}`\n"
            f"Net P/L: `${total_profit:+.2f}`\n"
            f"Outcome: {outcome_icon}"
        )

    return results, total_profit

# ─────────────────────────────────────────────────────────────────
#  UTILITY: verify a token is valid before trading
# ─────────────────────────────────────────────────────────────────

async def verify_token(token: str) -> dict:
    """
    Quick auth check. Returns the authorize response dict on success.
    Raises RuntimeError on failure.
    Use this before triggering a trade sequence to give the user
    a clean error message if their session has expired.
    """
    async with websockets.connect(DERIV_WS_URL, open_timeout=10) as ws:
        await ws.send(json.dumps({"authorize": token}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if "error" in msg:
            raise RuntimeError(f"Token invalid: {msg['error']['message']}")
        return msg["authorize"]

# ─────────────────────────────────────────────────────────────────
#  UTILITY: fetch available Step Index symbols at runtime
# ─────────────────────────────────────────────────────────────────

async def get_step_symbols() -> list[str]:
    """
    Queries Deriv's active_symbols endpoint and returns all Step Index
    symbols available for trading. Call this once at startup to confirm
    which symbols your account can trade.
    """
    async with websockets.connect(DERIV_WS_URL, open_timeout=10) as ws:
        await ws.send(json.dumps({
            "active_symbols": "brief",
            "product_type":   "basic",
        }))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        symbols = msg.get("active_symbols", [])
        step = [
            s["symbol"] for s in symbols
            if "step" in s.get("symbol", "").lower()
            or "step" in s.get("display_name", "").lower()
        ]
        log.info("Available Step Index symbols: %s", step)
        return step
