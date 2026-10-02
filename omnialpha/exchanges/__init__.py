"""Exchange adapters: unified market + trading contract."""
from __future__ import annotations

from .base import ExchangeClient, ExchangeError, SymbolMapper
from .registry import create_exchange, list_exchanges

__all__ = [
    "ExchangeClient",
    "ExchangeError",
    "SymbolMapper",
    "create_exchange",
    "list_exchanges",
]
