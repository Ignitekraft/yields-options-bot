from __future__ import annotations


class TelegramKeyboard:
    """Button layout used by the Telegram product interface."""

    @staticmethod
    def start_keyboard() -> list[list[str]]:
        return [["Connect Deriv"], ["Market Scanner"], ["Account Status"]]

    @staticmethod
    def status_keyboard() -> list[list[str]]:
        return [["Refresh"], ["Disconnect"]]
