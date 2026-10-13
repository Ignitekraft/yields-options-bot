"""Deriv API service namespace."""

from .market_data import MarketDataService
from .trading import DerivTradingService
from .websocket import AuthenticatedDerivWebSocket

__all__ = ["MarketDataService", "DerivTradingService", "AuthenticatedDerivWebSocket"]
