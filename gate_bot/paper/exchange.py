"""PaperExchange — 实现 ExchangeClient 全接口的本地模拟交易所 adapter。

行情/合约/费率委托给绑定的真实所（feed）；订单/持仓/资金/成交走本地 paper_account.db。
复刻交易所语义：精度校验、订单状态机、盘口价成交、强平、资金费率。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..exchanges.base import ExchangeClient, ExchangeError
from .engine import PaperEngine, _order_view, _price_order_view
from .risk import RiskEngine
from .store import (
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_OPEN,
    ORDER_PARTIALLY_FILLED,
    ORDER_REJECTED,
    PaperStore,
    new_order_id,
)
from .validate import PaperReject


class PaperExchange(ExchangeClient):
    name = "paper"
    supports_testnet = False
    supports_price_orders = True
    supports_margin_mode = True

    def __init__(
        self,
        env: str = "live",
        api_key: str = "",
        api_secret: str = "",
        *,
        store_path: Optional[Path] = None,
        feed: Optional[Any] = None,
        config: Optional[dict] = None,
        **kwargs: Any,
    ):
        super().__init__(env=env, api_key=api_key, api_secret=api_secret, **kwargs)
        if store_path is None:
            raise ValueError("paper: store_path required (per-bot paper_account.db)")
        self.store = PaperStore(Path(store_path))
        self.store.init_config(config)
        if feed is None:
            raise ValueError("paper: feed exchange required")
        self.feed = feed
        self.engine = PaperEngine(self.store, self.feed)
        self.risk = RiskEngine(self.store, self.feed, self.engine)
        self._lock = threading.RLock()

    # ── 行情（委托 feed）─────────────────────────────────
    def get_last_price(self, symbol: str) -> float:
        return self.feed.get_last_price(symbol)

    def get_ticker(self, symbol: str) -> dict:
        return self.feed.get_ticker(symbol)

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        return self.feed.get_klines(symbol, interval, limit)

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        return self.feed.get_orderbook_top(symbol, limit=limit)

    def get_contract(self, symbol: str) -> Any:
        return self.feed.get_contract(symbol)

    def get_contract_stats(self, symbol: str, limit: int = 1) -> list[dict]:
        try:
            return self.feed.get_contract_stats(symbol, limit=limit) or []
        except Exception:  # noqa: BLE001
            return []

    # ── 账户（本地）────────────────────────────────────
    def get_account(self) -> dict:
        with self._lock:
            return self.engine.get_account_view()

    def get_positions(self) -> list[dict]:
        with self._lock:
            return self.engine.get_positions_view()

    def get_available_usdt(self) -> float:
        acct = self.get_account()
        try:
            return float(acct.get("available") or 0)
        except (TypeError, ValueError):
            return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        lev = float(leverage)
        meta = self.feed.get_contract(symbol)
        cap = float(getattr(meta, "leverage_max", 100) or 100)
        if cap and lev > cap:
            raise ExchangeError(f"leverage too high: {lev} > {cap}", status=400, exchange="paper")
        self.store.update_account(leverage=lev)
        return {"symbol": symbol, "leverage": lev}

    def set_margin_mode(self, symbol: str, mode: str) -> Any:
        m = str(mode or "isolated").lower()
        if m not in ("isolated", "cross"):
            raise ExchangeError(f"invalid margin mode: {mode}", status=400, exchange="paper")
        self.store.update_account(margin_mode=m)
        return {"symbol": symbol, "margin_mode": m}

    def is_dual_position_mode(self) -> bool:
        return self.store.cfg_s("position_mode", "single") == "dual"

    def get_position_mode(self) -> str:
        return self.store.cfg_s("position_mode", "single")

    # ── 交易（本地）────────────────────────────────────
    def place_order(self, body: dict) -> dict:
        with self._lock:
            try:
                return self.engine.place_order(body)
            except PaperReject as e:
                raise ExchangeError(str(e), status=e.status, exchange="paper") from e

    def place_price_order(self, body: dict) -> dict:
        with self._lock:
            try:
                return self.engine.place_price_order(body)
            except PaperReject as e:
                raise ExchangeError(str(e), status=e.status, exchange="paper") from e

    def get_order(self, order_id: str) -> dict:
        o = self.store.get_order(str(order_id))
        if not o:
            raise ExchangeError("order not found", status=404, exchange="paper")
        return _order_view(o)

    def get_price_order(self, price_order_id: str) -> dict:
        o = self.store.get_price_order(str(price_order_id))
        if not o:
            raise ExchangeError("price order not found", status=404, exchange="paper")
        return _price_order_view(o)

    def list_orders(self, contract: Optional[str] = None) -> list:
        return [_order_view(o) for o in self.store.list_orders(contract=contract)]

    def list_price_orders(self, contract: Optional[str] = None) -> list:
        return [_price_order_view(o) for o in self.store.list_price_orders(contract=contract)]

    def cancel_order(self, order_id: str) -> Any:
        o = self.store.get_order(str(order_id))
        if not o:
            raise ExchangeError("order not found", status=404, exchange="paper")
        if o.get("status") in (ORDER_FILLED, ORDER_CANCELLED, ORDER_REJECTED):
            return o
        self.store.update_order(
            str(order_id), status=ORDER_CANCELLED, finish_time=int(time.time()),
        )
        return _order_view(self.store.get_order(str(order_id)))

    def cancel_all_orders(self, contract: str) -> Any:
        n = 0
        for o in self.store.list_orders(contract=contract, status=ORDER_OPEN):
            self.store.update_order(o["order_id"], status=ORDER_CANCELLED, finish_time=int(time.time()))
            n += 1
        for o in self.store.list_orders(contract=contract, status=ORDER_PARTIALLY_FILLED):
            self.store.update_order(o["order_id"], status=ORDER_CANCELLED, finish_time=int(time.time()))
            n += 1
        return {"cancelled": n}

    def cancel_price_order(self, order_id: str) -> Any:
        po = self.store.get_price_order(str(order_id))
        if not po:
            raise ExchangeError("price order not found", status=404, exchange="paper")
        if po.get("status") in ("filled", "cancelled", "rejected"):
            return po
        self.store.update_price_order(
            str(order_id), status="cancelled", finish_time=int(time.time()),
        )
        return _price_order_view(self.store.get_price_order(str(order_id)))

    def cancel_all_price_orders(self, contract: Optional[str] = None) -> Any:
        n = 0
        for po in self.store.list_price_orders(contract=contract, status="untriggered"):
            self.store.update_price_order(po["order_id"], status="cancelled", finish_time=int(time.time()))
            n += 1
        return {"cancelled": n}

    def close_position(self, contract: str, side: Optional[str] = None, size: int = 0) -> Any:
        with self._lock:
            poss = self.store.get_positions(contract)
            if not poss:
                raise ExchangeError("no position", status=400, exchange="paper")
            # side=long/short 过滤（dual 模式下 Executor 分别平多/空）
            target = None
            if side:
                want = 1 if str(side).lower() in ("long", "buy") else -1
                for p in poss:
                    if (float(p.get("size") or 0) > 0) == (want > 0):
                        target = p
                        break
            else:
                target = poss[0]
            if target is None:
                raise ExchangeError("position_empty", status=400, exchange="paper")
            cur = float(target.get("size") or 0)
            if not cur:
                raise ExchangeError("position_empty", status=400, exchange="paper")
            close_sz = abs(cur) if not size else min(abs(float(size)), abs(cur))
            body = {
                "contract": contract,
                "size": (-close_sz if cur > 0 else close_sz),
                "price": 0,
                "tif": "ioc",
                "reduce_only": 1,
                "text": "close_position",
            }
            try:
                return self.engine.place_order(body)
            except PaperReject as e:
                raise ExchangeError(str(e), status=e.status, exchange="paper") from e

    # ── 扩展（Executor 用）──────────────────────────────
    def place_trailing_order(self, body: dict) -> dict:
        """追踪单暂不支持，返回明确错误（与实盘 trail 搁置一致）。"""
        raise ExchangeError("trailing order unsupported in paper", status=400, exchange="paper")

    def stop_trailing_orders(self, contract: Optional[str] = None) -> Any:
        return {"stopped": 0}

    # ── 引擎钩子（paper-run 周期调用）────────────────────
    def tick(self) -> dict:
        """每轮：扫描触发单 → 撮合 open 单 → 强平 → 费率结算 → 估值。

        feed 行情瞬时故障不抛出，记 error 后继续（tick 线程持续跑）。
        """
        with self._lock:
            triggered, liqs, funds, snap = [], [], [], {}
            err = None
            try:
                triggered = self.engine.scan_price_orders()
                for o in self.store.list_orders(status=ORDER_OPEN):
                    try:
                        self.engine.match_order(o["order_id"])
                    except Exception:  # noqa: BLE001
                        pass
                liqs = self.risk.check_liquidations()
                funds = self.risk.settle_funding()
                snap = self.engine.recalc_equity()
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            return {"triggered": len(triggered), "liquidations": liqs,
                    "funding": funds, "equity": snap.get("equity"),
                    "error": err}
