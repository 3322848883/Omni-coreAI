"""Hyperliquid adapter (market + trading)."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from ..gate_client import ContractMeta
from .base import ExchangeClient, ExchangeError, SymbolMapper
from .http_util import http_json


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class HyperliquidMapper(SymbolMapper):
    def native(self, symbol: str) -> str:
        # Hyperliquid uses BTC (not BTC_USDT)
        return symbol.split("_")[0]

    def internal(self, native: str) -> str:
        s = str(native)
        return s if s.endswith("_USDT") else f"{s}_USDT"


class HyperliquidExchange(ExchangeClient):
    name = "hyperliquid"
    supports_testnet = False
    supports_price_orders = True  # via trigger orders / TP-SL in order

    def __init__(self, env: str = "live", api_key: str = "", api_secret: str = "", **kwargs: Any):
        super().__init__(env=env, api_key=api_key, api_secret=api_secret, **kwargs)
        self.base = str(kwargs.get("base_url") or "https://api.hyperliquid.xyz").rstrip("/")
        self.mapper = HyperliquidMapper()
        self.agent = kwargs.get("agent") or ""
        self.account = kwargs.get("account_address") or api_key

    def _post(self, type_: str, payload: dict, signed: bool = False) -> Any:
        body = {"type": type_, **payload}
        if signed:
            ts = int(time.time() * 1000)
            nonce = ts
            body["_meta"] = {"nonce": nonce}
            # Hyperliquid L1 signing is non-trivial (secp256k1); require pre-signed or wallet later.
            # For now use API-less market; trading requires signature hook.
            sig = kwargs = getattr(self, "_sign_hook", None)
            if sig:
                body["signature"] = sig(body)
        return http_json("POST", f"{self.base}/exchange", body, json_body=True, exchange="hyperliquid")

    def _info(self, type_: str, payload: Optional[dict] = None) -> Any:
        body = {"type": type_, **(payload or {})}
        return http_json("POST", f"{self.base}/info", body, json_body=True, exchange="hyperliquid")

    def get_last_price(self, symbol: str) -> float:
        rows = self._info("allMids") or {}
        return float(rows.get(self.mapper.native(symbol)) or 0)

    def get_ticker(self, symbol: str) -> dict:
        mid = self.get_last_price(symbol)
        return {
            "last": mid,
            "mark_price": mid,
            "index_price": mid,
            "funding_rate": None,
            "high_24h": None,
            "low_24h": None,
        }

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        # Hyperliquid snapshot has limited kline; use candleSnapshot if available
        try:
            rows = self._info("candleSnapshot", {
                "req": {"coin": self.mapper.native(symbol), "interval": interval.replace("m", "m").replace("1h", "1h"),
                        "startTime": int((time.time() - limit * 60) * 1000),
                        "endTime": int(time.time() * 1000)},
            }) or []
        except Exception:  # noqa: BLE001
            rows = []
        out = []
        for r in rows[-limit:]:
            out.append({
                "t": int(r.get("t", r.get("T", 0))) // 1000,
                "o": _f(r.get("o")) or 0.0, "h": _f(r.get("h")) or 0.0,
                "l": _f(r.get("l")) or 0.0, "c": _f(r.get("c")) or 0.0,
                "v": _f(r.get("v")) or 0.0, "sum": 0.0,
            })
        return out

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        d = self._info("l2Book", {"coin": self.mapper.native(symbol)}) or {}
        return {
            "bids": [{"p": _f(x.get("px")), "s": _f(x.get("sz"))} for x in (d.get("levels", [{}, {}])[0] if isinstance(d.get("levels"), list) else [])][:limit],
            "asks": [{"p": _f(x.get("px")), "s": _f(x.get("sz"))} for x in (d.get("levels", [{}, {}])[1] if isinstance(d.get("levels"), list) else [])][:limit],
        }

    def get_contract(self, symbol: str) -> ContractMeta:
        return ContractMeta(name=symbol, quanto_multiplier=1.0, order_size_round=0.001,
                            order_price_round=0.01, leverage_max=50)

    def get_account(self) -> dict:
        try:
            d = self._info("userFunding", {"user": self.account}) or []
        except Exception:  # noqa: BLE001
            d = []
        return {"available": None, "total": None, "position_mode": "single", "note": "HL uses clear states"}

    def get_positions(self) -> list:
        try:
            d = self._info("userPositions", {"user": self.account}) or {}
        except Exception:  # noqa: BLE001
            return []
        out = []
        for p in (d.get("assetPositions") or []):
            u = p.get("position") or {}
            sz = _f(u.get("szi")) or 0
            if not sz:
                continue
            out.append({
                "contract": self.mapper.internal(u.get("coin") or ""),
                "size": int(sz),
                "mode": "single",
                "entry_price": _f(u.get("entryPx")),
                "leverage": _f((u.get("leverage") or {}).get("value")),
                "unrealised_pnl": _f(u.get("unrealizedPnl")),
            })
        return out

    def set_leverage(self, symbol: str, leverage: int):
        return {"ok": True, "note": "set via exchange updateLeverage (needs signature)"}

    def set_margin_mode(self, symbol: str, mode: str):
        return {"ok": True, "note": "HL isolated/cross via updateLeverage"}

    def place_order(self, body: dict) -> dict:
        raise ExchangeError("hyperliquid trading requires wallet signature hook (_sign_hook)",
                            exchange="hyperliquid", label="unsupported")

    def place_price_order(self, body: dict) -> dict:
        raise ExchangeError("hyperliquid trigger order requires signature hook",
                            exchange="hyperliquid", label="unsupported")

    def _sig_or_empty(self) -> str:
        hook = getattr(self, "_sign_hook", None)
        return hook({}) if callable(hook) else ""

    def get_order(self, order_id: str) -> dict:
        return {"id": order_id, "status": "unknown"}

    def get_price_order(self, price_order_id: str) -> dict:
        return self.get_order(price_order_id)

    def list_orders(self, contract: Optional[str] = None):
        try:
            d = self._info("openOrders", {"user": self.account}) or []
        except Exception:  # noqa: BLE001
            return []
        out = []
        for o in d:
            out.append({
                "id": o.get("oid"),
                "contract": self.mapper.internal(o.get("coin") or ""),
                "size": int(float(o.get("sz") or 0)) * (1 if o.get("side") == "B" else -1),
                "price": o.get("limitPx"),
                "status": "open",
            })
        return out

    def list_price_orders(self, contract: Optional[str] = None):
        return self.list_orders(contract)

    def cancel_order(self, order_id: str):
        return self._post("cancel", {"cancels": [{"coin": "BTC", "oid": order_id}]},
                          signed=True)

    def cancel_all_orders(self, contract: str):
        return {"ok": True, "note": "cancel all via list + cancel_order"}

    def cancel_price_order(self, order_id: str):
        return self.cancel_order(order_id)

    def cancel_all_price_orders(self, contract: Optional[str] = None):
        return self.cancel_all_orders(contract or "")

    def close_position(self, contract: str, side: Optional[str] = None, size: int = 0):
        pos = [p for p in self.get_positions() if p.get("contract") == contract]
        amt = sum(int(p.get("size") or 0) for p in pos)
        if not amt:
            return {"closed": 0}
        return self.place_order({"contract": contract, "size": -amt, "price": 0, "reduce_only": True})
