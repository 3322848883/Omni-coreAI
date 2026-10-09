"""Bitget USDT-M futures adapter (market + trading)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

from ..gate_client import ContractMeta
from .base import ExchangeClient, SymbolMapper
from .http_util import http_json

log = logging.getLogger("omnialpha.exchanges.bitget")


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class BitgetExchange(ExchangeClient):
    name = "bitget"
    supports_testnet = True
    supports_price_orders = True

    def __init__(self, env: str = "live", api_key: str = "", api_secret: str = "",
                 passphrase: str = "", **kwargs: Any):
        super().__init__(env=env, api_key=api_key, api_secret=api_secret, **kwargs)
        self.passphrase = passphrase or kwargs.get("passphrase") or ""
        self.base = str(kwargs.get("base_url") or "https://api.bitget.com").rstrip("/")
        self.mapper = BitgetMapper()

    def _req(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False) -> Any:
        params = dict(params or {})
        ts = str(int(time.time() * 1000))
        body_s = json.dumps(params) if method != "GET" and params else ""
        headers = {"Content-Type": "application/json"}
        if signed:
            qs = ""
            if method == "GET" and params:
                from urllib.parse import urlencode

                qs = "?" + urlencode(params)
            sign_s = ts + method.upper() + (path + qs) + body_s
            sign = base64.b64encode(
                hmac.new(self.api_secret.encode(), sign_s.encode(), hashlib.sha256).digest()
            ).decode()
            headers.update({
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": sign,
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self.passphrase,
            })
            if self.env == "testnet":
                headers["paptrading"] = "1"
        if method == "GET":
            return http_json("GET", f"{self.base}{path}", params, headers=headers, exchange="bitget")
        return http_json(method, f"{self.base}{path}", params, headers=headers, json_body=True, exchange="bitget")

    def get_last_price(self, symbol: str) -> float:
        d = self._req("GET", "/api/v2/mix/market/ticker", {"productType": "USDT-FUTURES", "symbol": self.mapper.native(symbol)}) or {}
        row = (d.get("data") or [{}])[0]
        return float(row.get("lastPr") or 0)

    def get_ticker(self, symbol: str) -> dict:
        d = self._req("GET", "/api/v2/mix/market/ticker", {"productType": "USDT-FUTURES", "symbol": self.mapper.native(symbol)}) or {}
        row = (d.get("data") or [{}])[0]
        return {
            "last": _f(row.get("lastPr")),
            "mark_price": _f(row.get("markPrice")),
            "index_price": _f(row.get("indexPrice")),
            "funding_rate": _f(row.get("fundingRate")),
            "high_24h": _f(row.get("high24h")),
            "low_24h": _f(row.get("low24h")),
            "change_percentage": _f(row.get("change24h")),
            "volume_24h_quote": _f(row.get("baseVolume")),
            "highest_bid": _f(row.get("bidPr")),
            "lowest_ask": _f(row.get("askPr")),
        }

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        # Bitget mix candles: minute bars lowercase; hour/day uppercase (1H/4H/1D)
        iv = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1H", "4h": "4H", "1d": "1D",
        }.get(interval, "15m")
        end = int(time.time() * 1000)
        rows = []
        for _ in range(3):
            d = self._req("GET", "/api/v2/mix/market/candles", {
                "productType": "USDT-FUTURES", "symbol": self.mapper.native(symbol),
                "granularity": iv, "limit": min(limit, 200), "endTime": end,
            }) or {}
            rows = d.get("data") or []
            if rows:
                break
            end -= 60000
        out = []
        for r in rows:
            out.append({"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]),
                        "l": float(r[3]), "c": float(r[4]), "v": float(r[5]), "sum": 0.0,
                        "ema20": None, "atr14": None})
        return out

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        d = self._req("GET", "/api/v2/mix/market/merge-depth", {
            "productType": "USDT-FUTURES", "symbol": self.mapper.native(symbol), "limit": limit,
        }) or {}
        row = (d.get("data") or {})
        return {
            "bids": [{"p": _f(x[0]), "s": _f(x[1])} for x in (row.get("bids") or [])],
            "asks": [{"p": _f(x[0]), "s": _f(x[1])} for x in (row.get("asks") or [])],
        }

    def get_contract(self, symbol: str) -> ContractMeta:
        """从 Bitget 取**真实**合约元数据（原先对所有币硬编码 `quanto=1.0/精度1/杠杆100`）。

        最要紧的是 `sizeMultiplier`（**合约面值** = 1 张代表多少币）：它直接决定
        「张数 ↔ 名义金额」的换算，写错就是数量级级别的错。另有 `volumePlace` /
        `pricePlace`（数量/价格小数位）与 `maxLever`。

        字段名取自 Bitget v2 mix `/api/v2/mix/market/contracts`。**取不到任何一项就
        整体退回原默认**（不影响行情与下单），并留痕 —— 宁可元数据缺失，也不要拿
        一个猜出来的面值去算张数。
        """
        native = self.mapper.native(symbol)
        try:
            d = self._req("GET", "/api/v2/mix/market/contracts",
                          {"productType": "USDT-FUTURES", "symbol": native}) or {}
            rows = d.get("data") or []
            row = rows[0] if isinstance(rows, list) and rows else (
                rows if isinstance(rows, dict) else {})
            quanto = _f(row.get("sizeMultiplier"))
            vol_place = row.get("volumePlace")
            px_place = row.get("pricePlace")
            max_lev = _f(row.get("maxLever"))
            if not quanto:
                raise ValueError("sizeMultiplier 缺失")
            return ContractMeta(
                name=symbol,
                quanto_multiplier=float(quanto),
                order_size_round=(10.0 ** -int(vol_place)) if vol_place is not None else 1.0,
                order_price_round=(10.0 ** -int(px_place)) if px_place is not None else 0.1,
                leverage_max=float(max_lev) if max_lev else 100.0)
        except Exception as e:  # noqa: BLE001 — 取不到就用默认，不断链路
            log.warning("bitget get_contract(%s): 合约元数据取不到（%s），退回默认", symbol, e)
        return ContractMeta(name=symbol, quanto_multiplier=1.0, order_size_round=1.0,
                            order_price_round=0.1, leverage_max=100)

    def get_account(self) -> dict:
        d = self._req("GET", "/api/v2/mix/account/accounts", {"productType": "USDT-FUTURES"}, signed=True) or {}
        row = (d.get("data") or [{}])[0]
        return {"available": row.get("available"), "total": row.get("accountEquity"),
                "position_mode": "dual"}

    def get_positions(self) -> list:
        d = self._req("GET", "/api/v2/mix/positions/all-position", {"productType": "USDT-FUTURES"}, signed=True) or {}
        out = []
        for p in d.get("data") or []:
            sz = float(p.get("total") or 0)
            if sz == 0:
                continue
            side = 1 if p.get("holdSide") == "long" else -1
            out.append({
                "contract": self.mapper.internal(p.get("symbol") or ""),
                "size": int(sz) * side,
                "mode": "dual_long" if side > 0 else "dual_short",
                "entry_price": _f(p.get("averageOpenPrice")),
                "leverage": _f(p.get("leverage")),
                "unrealised_pnl": _f(p.get("unrealizedPL")),
            })
        return out

    def set_leverage(self, symbol: str, leverage: int):
        return self._req("POST", "/api/v2/mix/account/set-leverage", {
            "productType": "USDT-FUTURES", "symbol": self.mapper.native(symbol),
            "leverage": str(int(leverage)), "holdSide": "long",
        }, signed=True)

    def set_margin_mode(self, symbol: str, mode: str):
        return {"ok": True}

    def is_dual_position_mode(self) -> bool:
        return True

    def place_order(self, body: dict) -> dict:
        native = self.mapper.native(body.get("contract") or body.get("symbol") or "")
        size = int(body.get("size") or 0)
        params = {
            "productType": "USDT-FUTURES", "symbol": native,
            "side": "buy" if size > 0 else "sell",
            "tradeSide": "open", "orderType": "market" if str(body.get("price")) in ("0", "0.0", "") else "limit",
            "size": str(abs(size)),
        }
        if str(body.get("price")) not in ("0", "0.0", "", "None"):
            params["price"] = str(body.get("price"))
        if body.get("reduce_only"):
            params["tradeSide"] = "close"
        d = self._req("POST", "/api/v2/mix/order/place-order", params, signed=True) or {}
        row = (d.get("data") or [{}])[0]
        return {"id": row.get("orderId"), "order": row}

    def place_price_order(self, body: dict) -> dict:
        native = self.mapper.native(body.get("contract") or body.get("symbol") or "")
        trig = body.get("trigger") or {}
        ini = body.get("initial") or {}
        size = int(ini.get("size") or 0)
        params = {
            "productType": "USDT-FUTURES", "symbol": native,
            "side": "buy" if size > 0 else "sell", "tradeSide": "close",
            "orderType": "market", "size": str(abs(size)),
            "presetTakeProfitPrice": str(trig.get("price") if (trig.get("rule") == 1) else ""),
            "presetStopLossPrice": str(trig.get("price") if (trig.get("rule") == 2) else ""),
        }
        d = self._req("POST", "/api/v2/mix/order/place-order", params, signed=True) or {}
        row = (d.get("data") or [{}])[0]
        return {"id": row.get("orderId"), "raw": row}

    def get_order(self, order_id: str) -> dict:
        return self._req("GET", "/api/v2/mix/order/detail", {"orderId": order_id}, signed=True) or {}

    def get_price_order(self, price_order_id: str) -> dict:
        return self.get_order(price_order_id)

    def list_orders(self, contract: Optional[str] = None):
        params = {"productType": "USDT-FUTURES"}
        if contract:
            params["symbol"] = self.mapper.native(contract)
        d = self._req("GET", "/api/v2/mix/order/orders-pending", params, signed=True) or {}
        out = []
        for o in d.get("data") or []:
            sz = int(float(o.get("size") or 0))
            out.append({
                "id": o.get("orderId"),
                "text": o.get("clientOid"),
                "contract": self.mapper.internal(o.get("symbol") or ""),
                "size": sz if o.get("side") == "buy" else -sz,
                "price": o.get("price"),
                "left": sz,
                "status": o.get("state"),
            })
        return out

    def list_price_orders(self, contract: Optional[str] = None):
        return self.list_orders(contract)

    def cancel_order(self, order_id: str):
        return self._req("POST", "/api/v2/mix/order/cancel-order", {"orderId": order_id}, signed=True)

    def cancel_all_orders(self, contract: str):
        return self._req("POST", "/api/v2/mix/order/cancel-all-orders", {
            "productType": "USDT-FUTURES", "symbol": self.mapper.native(contract),
        }, signed=True)

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


class BitgetMapper(SymbolMapper):
    def native(self, symbol: str) -> str:
        # Bitget v2 mix symbol: BTCUSDT
        return symbol.replace("_", "")

    def internal(self, native: str) -> str:
        s = str(native)
        if "_" not in s and s.endswith("USDT"):
            s = s[:-4] + "_USDT"
        return s
