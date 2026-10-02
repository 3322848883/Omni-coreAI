"""Gate.io adapter — wraps existing GateClient (normalized)."""
from __future__ import annotations

from typing import Any, Optional

from ..gate_client import GateClient
from .base import ExchangeClient, SymbolMapper


class GateExchange(ExchangeClient):
    name = "gate"
    supports_testnet = True
    supports_price_orders = True

    def __init__(self, env: str = "live", api_key: str = "", api_secret: str = "", **kwargs: Any):
        super().__init__(env=env, api_key=api_key, api_secret=api_secret, **kwargs)
        self.mapper = SymbolMapper.identity("gate")
        self._c = GateClient(api_key, api_secret, env=env)

    def _s(self, symbol: str) -> str:
        return self.mapper.native(symbol)

    def get_last_price(self, symbol: str) -> float:
        return self._c.get_last_price(self._s(symbol))

    def get_ticker(self, symbol: str) -> dict:
        return self._c.get_ticker(self._s(symbol))

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        from ..strategist.market import fetch_rest_candles

        return fetch_rest_candles(self._c, self._s(symbol), interval, limit)

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        return self._c.get_orderbook_top(self._s(symbol), limit=limit)

    def get_contract(self, symbol: str):
        return self._c.get_contract(self._s(symbol))

    def get_contract_stats(self, symbol: str, limit: int = 1) -> list:
        return self._c.get_contract_stats(self._s(symbol), limit=limit)

    def get_account(self) -> dict:
        return self._c.get_account() or {}

    def get_positions(self) -> list:
        return self._c.get_positions() or []

    def set_leverage(self, symbol: str, leverage: int):
        return self._c.set_leverage(self._s(symbol), leverage)

    def set_margin_mode(self, symbol: str, mode: str):
        return self._c.set_margin_mode(self._s(symbol), mode)

    def is_dual_position_mode(self) -> bool:
        return self._c.is_dual_position_mode()

    def get_position_mode(self) -> str:
        return self._c.get_position_mode()

    def place_order(self, body: dict) -> dict:
        b = dict(body)
        b["contract"] = self._s(b.get("contract") or b.get("symbol") or "")
        return self._c.place_order(b)

    def place_price_order(self, body: dict) -> dict:
        b = dict(body)
        if "contract" not in b:
            b["contract"] = self._s(b.get("symbol") or "")
        return self._c.place_price_order(b)

    def get_order(self, order_id: str) -> dict:
        return self._c.get_order(order_id)

    def get_price_order(self, price_order_id: str) -> dict:
        return self._c.get_price_order(price_order_id)

    def list_orders(self, contract: Optional[str] = None):
        return self._c.list_orders(self._s(contract) if contract else None)

    def list_price_orders(self, contract: Optional[str] = None):
        return self._c.list_price_orders(self._s(contract) if contract else None)

    def cancel_order(self, order_id: str):
        return self._c.cancel_order(order_id)

    def cancel_all_orders(self, contract: str):
        return self._c.cancel_all_orders(self._s(contract))

    def cancel_price_order(self, order_id: str):
        return self._c.cancel_price_order(order_id)

    def cancel_all_price_orders(self, contract: Optional[str] = None):
        return self._c.cancel_all_price_orders(self._s(contract) if contract else None)

    def close_position(self, contract: str, side: Optional[str] = None, size: int = 0):
        return self._c.close_position(self._s(contract), side=side, size=size)

    def raw(self) -> GateClient:
        return self._c
