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

    def get_contract_stats(self, symbol: str, limit: int = 1, interval: str = "") -> list:
        return self._c.get_contract_stats(self._s(symbol), limit=limit, interval=interval)

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

    def list_my_trades(self, contract: Optional[str] = None, limit: int = 1000,
                       last_id: Optional[str] = None):
        """已成交明细（live 画像的盈亏数据源）。`contract=None` 拉全账户。

        **故意不进 `ExchangeClient` 基类**：只有 live 的画像投影用到它
        （`memory/exchange_pnl.py`），其余交易所的 live 未接 —— 进基类会强迫 5 个
        适配器写一遍 `NotImplementedError`，纯噪音。调用方按鸭子类型用，
        `exchange_pnl.sync` 对缺方法的客户端已经 try/except 兜住。
        """
        return self._c.list_my_trades(
            contract=self._s(contract) if contract else None,
            limit=limit, last_id=last_id)

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

    # ── 补齐透传：这 6 个此前没有包装，调用即 AttributeError ──
    # 生产调用点实测：`get_available_usdt`（executor 的 size_pct / margin_pct 分支，
    # 4 处）、`place_trailing_order` / `stop_trailing_orders`（`trail` / `cancel_trail_all`
    # 动作，都在 schema 的允许集里 → 可达）。`public_get` / `get_contracts` /
    # `rest_signed_request` 当前调用方少，但一并补齐。
    #
    # 这与当初**触发子系统整个死掉 178 个周期**是同一类问题（那边是 `public_get`
    # 缺失、被 `except` 兜底成 False）—— **适配器必须暴露底层客户端的完整接口，
    # 否则调用方在运行时才炸**。测试里的假客户端实现了完整接口，所以这个缺口
    # 在单测里完全隐形。现在由 `tests/test_exchange_proxy_complete.py` 守着。

    def get_available_usdt(self) -> float:
        return self._c.get_available_usdt()

    def get_contracts(self, max_age_sec: int = 3600) -> dict:
        return self._c.get_contracts(max_age_sec=max_age_sec)

    def place_trailing_order(self, body: dict) -> dict:
        b = dict(body)
        b["contract"] = self._s(b.get("contract") or b.get("symbol") or "")
        return self._c.place_trailing_order(b)

    def stop_trailing_orders(self, contract: Optional[str] = None):
        return self._c.stop_trailing_orders(self._s(contract) if contract else None)

    def public_get(self, path: str, query_string: str = "", attempts: int = 3) -> Any:
        return self._c.public_get(path, query_string, attempts=attempts)

    def rest_signed_request(
        self, method: str, path: str, query_string: str = "", body: Any = None
    ) -> Any:
        return self._c.rest_signed_request(method, path, query_string=query_string, body=body)

    def raw(self) -> GateClient:
        return self._c
