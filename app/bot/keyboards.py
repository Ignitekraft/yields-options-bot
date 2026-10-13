from __future__ import annotations

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:  # pragma: no cover
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None


class TelegramKeyboard:
    """Inline button layouts for the Yields dashboard and Auto Trade flow."""

    @staticmethod
    def start_keyboard() -> object:
        if InlineKeyboardMarkup is None:
            return [["🔐 Connect Deriv"], ["ℹ️ How Yields Works"]]
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔐 Connect Deriv", callback_data="START_CONNECT")],
                [InlineKeyboardButton("ℹ️ How Yields Works", callback_data="START_INFO")],
            ]
        )

    @staticmethod
    def dashboard_keyboard() -> object:
        if InlineKeyboardMarkup is None:
            return [["🤖 Auto Trade"], ["💰 Balance"], ["📊 Status"], ["⚙️ Settings"], ["🔌 Disconnect"]]
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🤖 Auto Trade", callback_data="START_AUTO_TRADE")],
                [
                    InlineKeyboardButton("💰 Balance", callback_data="START_BALANCE"),
                    InlineKeyboardButton("📊 Status", callback_data="START_STATUS"),
                ],
                [
                    InlineKeyboardButton("⚙️ Settings", callback_data="START_SETTINGS"),
                    InlineKeyboardButton("🔌 Disconnect", callback_data="START_LOGOUT"),
                ],
            ]
        )

    @staticmethod
    def auto_trade_keyboard() -> object:
        if InlineKeyboardMarkup is None:
            return [["🚀 Start Scan"], ["✕ Cancel"]]
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🚀 Start Scan", callback_data="AUTO_TRADE_START_SCAN")],
                [InlineKeyboardButton("✕ Cancel", callback_data="AUTO_TRADE_CANCEL")],
            ]
        )

    @staticmethod
    def stake_keyboard() -> object:
        if InlineKeyboardMarkup is None:
            return [["$1"], ["$2"], ["$5"], ["$10"], ["Custom"]]
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("$1", callback_data="AUTO_TRADE_STAKE_1")],
                [InlineKeyboardButton("$2", callback_data="AUTO_TRADE_STAKE_2")],
                [InlineKeyboardButton("$5", callback_data="AUTO_TRADE_STAKE_5")],
                [InlineKeyboardButton("$10", callback_data="AUTO_TRADE_STAKE_10")],
                [InlineKeyboardButton("Custom Stake", callback_data="AUTO_TRADE_STAKE_CUSTOM")],
            ]
        )

    @staticmethod
    def status_keyboard() -> object:
        if InlineKeyboardMarkup is None:
            return [["Refresh"], ["Disconnect"]]
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Refresh", callback_data="START_STATUS")],
                [InlineKeyboardButton("Disconnect", callback_data="START_LOGOUT")],
            ]
        )
