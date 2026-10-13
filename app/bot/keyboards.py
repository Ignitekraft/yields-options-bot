from __future__ import annotations

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:  # pragma: no cover
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None


class TelegramKeyboard:
    """Inline keyboard layouts for the Yields dashboard and Auto Trade flow."""

    @staticmethod
    def _keyboard(rows: list[list[tuple[str, str]]]) -> object:
        if InlineKeyboardMarkup is None:
            return [[label for label, _ in row] for row in rows]

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(label, callback_data=data)
                    for label, data in row
                ]
                for row in rows
            ]
        )

    @staticmethod
    def start_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("🔐 Connect Deriv", "START_CONNECT")],
                [("ℹ️ How Yields Works", "START_INFO")],
            ]
        )

    @staticmethod
    def dashboard_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("🤖 Auto Trade", "START_AUTO_TRADE")],
                [
                    ("💰 Balance", "START_BALANCE"),
                    ("📊 Status", "START_STATUS"),
                ],
                [
                    ("⚙️ Account Mode", "START_SETTINGS"),
                    ("🔌 Disconnect", "START_LOGOUT"),
                ],
            ]
        )

    @staticmethod
    def account_mode_keyboard(current_mode: str) -> object:
        mode = str(current_mode or "demo").lower()

        demo_label = "🟡 Demo ✓" if mode == "demo" else "🟡 Demo"
        real_label = "🟢 Real ✓" if mode == "real" else "🟢 Real"

        return TelegramKeyboard._keyboard(
            [
                [(demo_label, "ACCOUNT_MODE_DEMO")],
                [(real_label, "ACCOUNT_MODE_REAL")],
                [("↩️ Back to Dashboard", "START_DASHBOARD")],
            ]
        )

    @staticmethod
    def real_mode_confirmation_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("✅ Confirm Real Mode", "ACCOUNT_MODE_REAL_CONFIRM")],
                [("✕ Cancel", "START_SETTINGS")],
            ]
        )

    @staticmethod
    def auto_trade_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("🚀 Start Scan", "AUTO_TRADE_START_SCAN")],
                [("✕ Cancel", "AUTO_TRADE_CANCEL")],
            ]
        )

    @staticmethod
    def stake_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("$1", "AUTO_TRADE_STAKE_1")],
                [("$2", "AUTO_TRADE_STAKE_2")],
                [("$5", "AUTO_TRADE_STAKE_5")],
                [("$10", "AUTO_TRADE_STAKE_10")],
                [("✕ Cancel", "AUTO_TRADE_CANCEL")],
            ]
        )

    @staticmethod
    def confirmation_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("✅ Confirm & Start", "AUTO_TRADE_CONFIRM")],
                [("✕ Cancel", "AUTO_TRADE_CANCEL")],
            ]
        )

    @staticmethod
    def status_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("Refresh", "START_STATUS")],
                [("Disconnect", "START_LOGOUT")],
            ]
        )