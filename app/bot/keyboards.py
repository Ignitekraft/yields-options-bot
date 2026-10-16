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
                [("💰 Balance", "START_BALANCE"), ("📊 Status", "START_STATUS")],
                [("❓ FAQ", "START_FAQ"), ("📈 Stats", "START_STATS")],
                [("⚙️ Account Mode", "START_SETTINGS"), ("🔌 Disconnect", "START_LOGOUT")],
            ]
        )

    @staticmethod
    def menu_keyboard(community_url: str) -> object:
        rows = [
            [("🚀 Auto Trade", "START_AUTO_TRADE")],
            [("💎 Get Tokens", "MENU_GET_TOKENS"), ("🌍 Community", community_url)],
            [("❓ Faq", "MENU_FAQ")],
            [("🎁 Invite", "MENU_INVITE")],
        ]
        if InlineKeyboardMarkup is None:
            return [[label for label, _ in row] for row in rows]

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        label,
                        url=value if label == "🌍 Community" else None,
                        callback_data=None if label == "🌍 Community" else value,
                    )
                    for label, value in row
                ]
                for row in rows
            ]
        )

    @staticmethod
    def low_token_keyboard(support_url: str) -> object:
        if InlineKeyboardMarkup is None:
            return [
                ["🚀 Trade Anyway", "💎 Get Tokens"],
                ["🏠 Main Menu", "💬 Message Sammie"],
            ]

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🚀 Trade Anyway",
                        callback_data="AUTO_TRADE_TRADE_ANYWAY",
                    ),
                    InlineKeyboardButton(
                        "💎 Get Tokens",
                        callback_data="MENU_GET_TOKENS",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Main Menu",
                        callback_data="MENU_MAIN",
                    ),
                    InlineKeyboardButton(
                        "💬 Message Sammie",
                        url=support_url,
                    ),
                ],
            ]
        )

    @staticmethod
    def connect_keyboard(url: str | None = None) -> object:
        if InlineKeyboardMarkup is None:
            return [["🔐 Open Deriv", "🏠 Main Menu"]]

        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔐 Open Deriv", url=url or "https://deriv.com")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="MENU_MAIN")],
            ]
        )

    @staticmethod
    def account_mode_keyboard(current_mode: str) -> object:
        mode = str(current_mode or "demo").lower()
        demo_label = "🟠 Demo ✓" if mode == "demo" else "🟠 Demo"
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
        if InlineKeyboardMarkup is None:
            return [["🟢 0.1%", "🟢 0.5%", "🟠 1%"]]

        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "🟢 0.1%",
                    callback_data="AUTO_TRADE_STAKE_0_1",
                    style="success",
                ),
                InlineKeyboardButton(
                    "🟢 0.5%",
                    callback_data="AUTO_TRADE_STAKE_0_5",
                    style="success",
                ),
                InlineKeyboardButton(
                    "🟠 1%",
                    callback_data="AUTO_TRADE_STAKE_1",
                ),
            ]]
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
                [("🔄 Refresh", "START_STATUS")],
                [("💰 Balance", "START_BALANCE")],
                [("❓ FAQ", "START_FAQ")],
                [("📈 Stats", "START_STATS")],
                [("🔌 Disconnect", "START_LOGOUT")],
            ]
        )