"""Bybit perpetual adapter (public + trading)."""
from __future__ import annotations

import hashlib
import hmac
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


class BybitExchange(ExchangeClient):
    name = "bybit"
    supports_testnet = True
    supports_price_orders = True

    def __init__(self, env: str = "live", api_key: str = "", api_secret: str = "", **kwargs: Any):
        super().__init__(env=env, api_key=api_key, api_secret=api_secret, **kwargs)
        self.base = str(kwargs.get("base_url") or (
            "https://api-testnet.bybit.com" if self.env == "testnet" else "https://api.bybit.com"
        )).rstrip("/")
        self.mapper = SymbolMapper.strip_quote("bybit", "USDT")

    def _req(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False) -> Any:
        params = dict(params or {})
        headers = {}
        if signed:
            ts = str(int(time.time() * 1000))
            body = json.dumps(params) if method != "GET" else ""
            qs = "&".join(f"{k}={params[k]}" for k in sorted(params)) if method == "GET" else ""
            recv = headers.get("X-BAPI-RECV-WINDOW", "5000")
            sign_s = ts + self.api_key + recv + qs + body
            sign = hmac.new(self.api_secret.encode(), sign_s.encode(), hashlib.sha256).hexdigest()
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-SIGN": sign,
                "X-BAPI-RECV-WINDOW": "5000",
                "Content-Type": "application/json",
            }
            if method == "GET":
                from urllib.parse import urlencode

                return http_json("GET", f"{self.base}{path}?{urlencode(params)}", headers=headers, exchange="bybit")
            return http_json("POST", f"{self.base}{path}", params, headers=headers, json_body=True, exchange="bybit")
        from urllib.parse import urlencode

        if method == "GET":
            return http_json("GET", f"{self.base}{path}?{urlencode(params)}", exchange="bybit")
        return http_json(method, f"{self.base}{path}", params, json_body=True, exchange="bybit")

    def get_last_price(self, symbol: str) -> float:
        d = self._req("GET", "/v5/market/tickers", {"category": "linear", "symbol": self.mapper.native(symbol)}) or {}
        row = ((d.get("result") or {}).get("list") or [{}])[0]
        return float(row.get("lastPrice") or 0)

    def get_ticker(self, symbol: str) -> dict:
        d = self._req("GET", "/v5/market/tickers", {"category": "linear", "symbol": self.mapper.native(symbol)}) or {}
        row = ((d.get("result") or {}).get("list") or [{}])[0]
        return {
            "last": _f(row.get("lastPrice")),
            "mark_price": _f(row.get("markPrice")),
            "index_price": _f(row.get("indexPrice")),
            "funding_rate": _f(row.get("fundingRate")),
            "high_24h": _f(row.get("highPrice24h")),
            "low_24h": _f(row.get("lowPrice24h")),
            "change_percentage": _f(row.get("price24hPcnt")),
            "volume_24h_quote": _f(row.get("turnover24h")),
            "highest_bid": _f(row.get("bid1Price")),
            "lowest_ask": _f(row.get("ask1Price")),
        }

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        iv = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}.get(interval, "15m")
        d = self._req("GET", "/v5/market/kline", {
            "category": "linear", "symbol": self.mapper.native(symbol), "interval": iv, "limit": limit,
        }) or {}
        rows = ((d.get("result") or {}).get("list") or [])
        out = []
        for r in rows:
            out.append({"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]),
                        "l": float(r[3]), "c": float(r[4]), "v": float(r[5]), "sum": float(r[6] or 0),
                        "ema20": None, "atr14": None})
        return list(reversed(out))

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        d = self._req("GET", "/v5/market/orderbook", {
            "category": "linear", "symbol": self.mapper.native(symbol), "limit": limit,
        }) or {}
        row = d.get("result") or {}
        return {
            "bids": [{"p": _f(x[0]), "s": _f(x[1])} for x in (row.get("b") or [])],
            "asks": [{"p": _f(x[0]), "s": _f(x[1])} for x in (row.get("a") or [])],
        }

    def get_contract(self, symbol: str) -> ContractMeta:
        d = self._req("GET", "/v5/market/instruments-info", {
            "category": "linear", "symbol": self.mapper.native(symbol),
        }) or {}
        row = ((d.get("result") or {}).get("list") or [{}])[0]
        return ContractMeta(
            name=symbol,
            quanto_multiplier=float(_f(row.get("lotSizeFilter", {}).get("qtyStep")) or 1) and 1.0,
            order_size_round=float(_f(row.get("lotSizeFilter", {}).get("qtyStep")) or 1),
            order_price_round=float(_f(row.get("priceFilter", {}).get("tickSize")) or 0.1),
            leverage_max=int(_f(row.get("leverageFilter", {}).get("maxLeverage")) or 100),
        )

    def get_account(self) -> dict:
        d = self._req("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, signed=True) or {}
        row = ((d.get("result") or {}).get("list") or [{}])[0]
        return {"available": row.get("totalAvailableBalance"), "total": row.get("totalEquity"),
                "position_mode": "dual"}

    def get_positions(self) -> list:
        d = self._req("GET", "/v5/position/list", {"category": "linear"}, signed=True) or {}
        out = []
        for p in ((d.get("result") or {}).get("list") or []):
            sz = float(p.get("size") or 0)
            if sz == 0:
                continue
            side = 1 if p.get("side") == "Buy" else -1
            out.append({
                "contract": self.mapper.internal(p.get("symbol") or ""),
                "size": int(sz) * side,
                "mode": "dual_long" if side > 0 else "dual_short",
                "entry_price": _f(p.get("avgPrice")),
                "leverage": _f(p.get("leverage")),
                "unrealised_pnl": _f(p.get("unrealisedPnl")),
                "liq_price": _f(p.get("liqPrice")),
                "margin": _f(p.get("positionIM")),
            })
        return out

    def set_leverage(self, symbol: str, leverage: int):
        return self._req("POST", "/v5/position/set-leverage", {
            "category": "linear", "symbol": self.mapper.native(symbol),
            "buyLeverage": str(int(leverage)), "sellLeverage": str(int(leverage)),
        }, signed=True)

    def set_margin_mode(self, symbol: str, mode: str):
        return {"ok": True, "note": "bybit trade mode set via set-margin-mode separately"}

    def is_dual_position_mode(self) -> bool:
        return True

    def place_order(self, body: dict) -> dict:
        native = self.mapper.native(body.get("contract") or body.get("symbol") or "")
        size = int(body.get("size") or 0)
        tif = (body.get("tif") or "GTC").upper()
        order_type = "Market" if str(body.get("price")) in ("0", "0.0", "", "None") else "Limit"
        params = {
            "category": "linear",
            "symbol": native,
            "side": "Buy" if size > 0 else "Sell",
            "qty": str(abs(size)),
            "orderType": order_type,
            "timeInForce": tif if tif in ("GTC", "IOC", "FOK", "PostOnly") else "GTC",
        }
        if order_type == "Limit":
            params["price"] = str(body.get("price"))
        if body.get("reduce_only"):
            params["reduceOnly"] = "true"
        d = self._req("POST", "/v5/order/create", params, signed=True) or {}
        return {"id": ((d.get("result") or {}).get("orderId")), "order": d.get("result")}

    def place_price_order(self, body: dict) -> dict:
        native = self.mapper.native(body.get("contract") or body.get("symbol") or "")
        trig = body.get("trigger") or {}
        ini = body.get("initial") or {}
        size = int(ini.get("size") or 0)
        params = {
            "category": "linear", "symbol": native,
            "side": "Buy" if size > 0 else "Sell",
            "qty": str(abs(size)), "orderType": "Market",
            "triggerPrice": str(trig.get("price")), "reduceOnly": "true",
        }
        d = self._req("POST", "/v5/order/create", params, signed=True) or {}
        return {"id": ((d.get("result") or {}).get("orderId")), "raw": d}

    def get_order(self, order_id: str) -> dict:
        return self._req("GET", "/v5/order/detail", {"category": "linear", "orderId": order_id}, signed=True) or {}

    def get_price_order(self, price_order_id: str) -> dict:
        return self.get_order(price_order_id)

    def list_orders(self, contract: Optional[str] = None):
        params = {"category": "linear"}
        if contract:
            params["symbol"] = self.mapper.native(contract)
        d = self._req("GET", "/v5/order/realtime", params, signed=True) or {}
        out = []
        for o in ((d.get("result") or {}).get("list") or []):
            qty = int(float(o.get("qty") or 0))
            out.append({
                "id": o.get("orderId"),
                "text": o.get("orderLinkId"),
                "contract": self.mapper.internal(o.get("symbol") or ""),
                "size": qty if o.get("side") == "Buy" else -qty,
                "price": o.get("price"),
                "left": qty - int(float(o.get("cumExecQty") or 0)),
                "status": o.get("orderStatus"),
            })
        return out

    def list_price_orders(self, contract: Optional[str] = None):
        return self.list_orders(contract)

    def cancel_order(self, order_id: str):
        return self._req("POST", "/v5/order/cancel", {"category": "linear", "orderId": order_id}, signed=True)

    def cancel_all_orders(self, contract: str):
        return self._req("POST", "/v5/order/cancel-all", {
            "category": "linear", "symbol": self.mapper.native(contract),
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
        return self.place_order({"contract": contract, "size": -amt, "price": 0, "tif": "IOC", "reduce_only": True})
