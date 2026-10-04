"""Unified exchange contract (market + trading)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class ExchangeError(Exception):
    def __init__(self, message: str, status: int = 0, label: str = "", exchange: str = ""):
        super().__init__(message)
        self.status = status
        self.label = label
        self.exchange = exchange


@dataclass
class SymbolMapper:
    """Internal BTC_USDT <-> exchange-specific symbols."""

    name: str = ""
    to_native: dict = field(default_factory=dict)
    to_internal: dict = field(default_factory=dict)

    @classmethod
    def identity(cls, name: str) -> "SymbolMapper":
        return cls(name=name)

    def native(self, symbol: str) -> str:
        if not self.to_native:
            return symbol
        return self.to_native.get(symbol, symbol)

    def internal(self, native: str) -> str:
        if not self.to_internal:
            return native
        return self.to_internal.get(native, native)

    @classmethod
    def strip_quote(cls, name: str, quote: str = "USDT") -> "SymbolMapper":
        """BTC_USDT <-> BTCUSDT (or any underscore-joined pair)."""

        def native(sym: str) -> str:
            return sym.replace("_", "")

        def internal(sym: str) -> str:
            if "_" in sym:
                return sym
            if sym.endswith(quote):
                return f"{sym[:-len(quote)]}_{quote}"
            return sym

        m = cls(name=name)
        m.native = native  # type: ignore[method-assign]
        m.internal = internal  # type: ignore[method-assign]
        return m


class ExchangeClient:
    """Abstract adapter. Subclasses implement Gate-normalized fields."""

    name: str = "base"
    supports_testnet: bool = True
    supports_price_orders: bool = True  # trigger TP/SL
    supports_margin_mode: bool = True

    def __init__(self, env: str = "live", api_key: str = "", api_secret: str = "", **kwargs: Any):
        self.env = (env or "live").lower()
        self.api_key = api_key
        self.api_secret = api_secret
        self._opts = kwargs

    # ── market ─────────────────────────────────────────
    def get_last_price(self, symbol: str) -> float:
        raise NotImplementedError

    def get_ticker(self, symbol: str) -> dict:
        raise NotImplementedError

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        """Return [{t,o,h,l,c,v,sum,ema20,atr14}] — t unix seconds.

        Field shape is pinned to Gate's candlesticks REST (Gate baseline):
        ema20/atr14 are None at fetch time; indicators are filled by the
        consumer (market.attach_indicators / pa kline writer).
        """
        raise NotImplementedError

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        """Return {bids:[{p,s}], asks:[{p,s}]}"""
        raise NotImplementedError

    def get_contract(self, symbol: str) -> Any:
        """ContractMeta-like: quanto_multiplier, order_size_round, order_price_round, leverage_max"""
        raise NotImplementedError

    def get_contract_stats(self, symbol: str, limit: int = 1, interval: str = "") -> list[dict]:
        return []

    # ── account / positions ────────────────────────────
    def get_account(self) -> dict:
        raise NotImplementedError

    def get_positions(self) -> list[dict]:
        raise NotImplementedError

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        raise NotImplementedError

    def set_margin_mode(self, symbol: str, mode: str) -> Any:
        raise NotImplementedError

    def is_dual_position_mode(self) -> bool:
        return False

    def get_position_mode(self) -> str:
        return "single"

    # ── trading ────────────────────────────────────────
    def place_order(self, body: dict) -> dict:
        """Normalized body: contract, size(+buy/-sell), price, tif / type."""
        raise NotImplementedError

    def place_price_order(self, body: dict) -> dict:
        """Trigger order (TP/SL)."""
        raise NotImplementedError

    def get_order(self, order_id: str) -> dict:
        raise NotImplementedError

    def get_price_order(self, price_order_id: str) -> dict:
        raise NotImplementedError

    def list_orders(self, contract: Optional[str] = None) -> list:
        raise NotImplementedError

    def list_price_orders(self, contract: Optional[str] = None) -> list:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> Any:
        raise NotImplementedError

    def cancel_all_orders(self, contract: str) -> Any:
        raise NotImplementedError

    def cancel_price_order(self, order_id: str) -> Any:
        raise NotImplementedError

    def cancel_all_price_orders(self, contract: Optional[str] = None) -> Any:
        raise NotImplementedError

    def close_position(self, contract: str, side: Optional[str] = None, size: int = 0) -> Any:
        raise NotImplementedError

    def banner(self) -> str:
        return f"[{self.name.upper()}:{self.env}]"
