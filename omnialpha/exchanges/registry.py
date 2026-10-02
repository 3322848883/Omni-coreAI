"""Exchange registry / factory."""
from __future__ import annotations

from typing import Optional

from .base import ExchangeClient

_EXCHANGES = {
    "gate": "omnialpha.exchanges.gate:GateExchange",
    "binance": "omnialpha.exchanges.binance:BinanceExchange",
    "okx": "omnialpha.exchanges.okx:OkxExchange",
    "bybit": "omnialpha.exchanges.bybit:BybitExchange",
    "bitget": "omnialpha.exchanges.bitget:BitgetExchange",
    "hyperliquid": "omnialpha.exchanges.hyperliquid:HyperliquidExchange",
}


def list_exchanges() -> list[str]:
    return list(_EXCHANGES)


def create_exchange(
    name: str,
    env: str = "live",
    api_key: str = "",
    api_secret: str = "",
    **kwargs,
) -> ExchangeClient:
    key = (name or "gate").strip().lower()
    if key not in _EXCHANGES:
        raise ValueError(f"unknown exchange {name!r}; known: {list_exchanges()}")
    mod_path, cls_name = _EXCHANGES[key].split(":")
    import importlib

    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(env=env, api_key=api_key, api_secret=api_secret, **kwargs)
