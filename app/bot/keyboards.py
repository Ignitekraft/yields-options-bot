from __future__ import annotations

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:  # pragma: no cover
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None


class TelegramKeyboard:
    """Inline keyboard layouts for the Yields dashboard and Auto Trade flow."""

    @staticmethod
    def _auto_trade_button(locked_seconds: int | None = None) -> tuple[str, str]:
        if locked_seconds is None:
            return ("🤖 Auto Trade", "START_AUTO_TRADE")
        seconds = max(0, int(locked_seconds))
        minutes, remaining_seconds = divmod(seconds, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            countdown = f"{hours}h {minutes:02d}m"
        else:
            countdown = f"{minutes}m {remaining_seconds:02d}s"
        return (
            f"🔒 Auto Trade · {countdown}",
            "AUTO_TRADE_LIMIT_INFO",
        )

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
    def dashboard_keyboard(locked_seconds: int | None = None) -> object:
        return TelegramKeyboard._keyboard(
            [
                [TelegramKeyboard._auto_trade_button(locked_seconds)],
                [("💰 Balance", "START_BALANCE"), ("📊 Status", "START_STATUS")],
                [("❓ FAQ", "START_FAQ"), ("📈 Stats", "START_STATS")],
                [("⚙️ Account Mode", "START_SETTINGS"), ("🔌 Disconnect", "START_LOGOUT")],
            ]
        )

    @staticmethod
    def menu_keyboard(
        community_url: str,
        locked_seconds: int | None = None,
    ) -> object:
        rows = [
            [
                (
                    ("🚀 Auto Trade", "START_AUTO_TRADE")
                    if locked_seconds is None
                    else TelegramKeyboard._auto_trade_button(locked_seconds)
                )
            ],
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
    def welcome_gift_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [[("🎁 Accept 200 Tokens", "WELCOME_GIFT_ACCEPT_200")]]
        )

    @staticmethod
    def referral_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("🏦 Set / Update Payout Details", "REFERRAL_PAYOUT_DETAILS")],
                [("💸 Withdraw Available Earnings", "REFERRAL_WITHDRAW")],
                [("🏠 Main Menu", "MENU_MAIN")],
            ]
        )

    @staticmethod
    def token_store_keyboard() -> object:
        rows = [
            [("50 Tokens", "TOKEN_PACKAGE_50")],
            [("100 Tokens", "TOKEN_PACKAGE_100")],
            [("200 Tokens", "TOKEN_PACKAGE_200")],
            [("500 Tokens", "TOKEN_PACKAGE_500")],
            [("🏠 Main Menu", "MENU_MAIN")],
        ]
        return TelegramKeyboard._keyboard(rows)

    @staticmethod
    def token_purchase_keyboard(payment_url: str) -> object:
        if InlineKeyboardMarkup is None:
            return [["💳 Pay in Whop"], ["↩️ Back to Tokens"]]

        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Pay in Whop", url=payment_url)],
                [
                    InlineKeyboardButton(
                        "↩️ Back to Tokens",
                        callback_data="MENU_GET_TOKENS",
                    )
                ],
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
    def exhausted_token_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [
                [("💎 Get Tokens", "MENU_GET_TOKENS")],
                [("🏠 Main Menu", "MENU_MAIN")],
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
                [("📷 Choose Chart Market", "AUTO_TRADE_START_SCAN")],
                [("✕ Cancel", "AUTO_TRADE_CANCEL")],
            ]
        )

    @staticmethod
    def chart_market_keyboard(markets: tuple[str, ...]) -> object:
        labels = {
            "step_100": "Step Index 100",
            "step_200": "Step Index 200",
            "step_300": "Step Index 300",
            "step_400": "Step Index 400",
            "step_500": "Step Index 500",
        }
        rows = [
            [(labels[symbol], f"AUTO_TRADE_CHART_{symbol}")]
            for symbol in markets
            if symbol in labels
        ]
        rows.append([("✕ Cancel", "AUTO_TRADE_CANCEL")])
        return TelegramKeyboard._keyboard(rows)

    @staticmethod
    def stake_keyboard(
        symbol: str | None = None,
        direction: str | None = None,
    ) -> object:
        selection_suffix = ""
        if symbol and direction:
            selection_suffix = f"_{symbol}_{direction.upper()}"

        if InlineKeyboardMarkup is None:
            return [
                ["🟢 0.1%", "🟢 0.5%", "🟠 1%"],
                ["✏️ Custom amount"],
            ]

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 0.1%",
                        callback_data=f"AUTO_TRADE_STAKE_0_1{selection_suffix}",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        "🟢 0.5%",
                        callback_data=f"AUTO_TRADE_STAKE_0_5{selection_suffix}",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        "🟠 1%",
                        callback_data=f"AUTO_TRADE_STAKE_1{selection_suffix}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Custom amount",
                        callback_data=f"AUTO_TRADE_CUSTOM_STAKE{selection_suffix}",
                    )
                ],
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
    def custom_stake_prompt_keyboard() -> object:
        return TelegramKeyboard._keyboard(
            [[("✕ Cancel", "AUTO_TRADE_CANCEL")]]
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