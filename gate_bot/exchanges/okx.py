"""OKX perpetual swaps adapter."""
from __future__ import annotations

import base64
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


class OkxExchange(ExchangeClient):
    name = "okx"
    supports_testnet = True
    supports_price_orders = True

    def __init__(self, env: str = "live", api_key: str = "", api_secret: str = "",
                 passphrase: str = "", **kwargs: Any):
        super().__init__(env=env, api_key=api_key, api_secret=api_secret, **kwargs)
        self.passphrase = passphrase or kwargs.get("passphrase") or ""
        self.base = str(kwargs.get("base_url") or (
            "https://www.okx.com" if self.env == "live" else "https://www.okx.com"
        )).rstrip("/")
        self.mapper = OkxMapper()

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = f"{ts}{method.upper()}{path}{body}"
        mac = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _req(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False,
             json_body: bool = True) -> Any:
        params = dict(params or {})
        body_s = json.dumps(params) if (params and method != "GET") else ""
        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 gate-signal-bot"}
        url = path if path.startswith("http") else f"{self.base}{path}"
        if signed:
            ts = str(int(time.time() * 1000))  # ms
            if method == "GET" and params:
                from urllib.parse import urlencode

                path = path + "?" + urlencode(params)
                url = f"{self.base}{path}"
                body_s = ""
            headers.update({
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": self._sign(ts, method, path, body_s),
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
            })
            if self.env == "testnet":
                headers["x-simulated-trading"] = "1"
        return http_json(
            method,
            url,
            params if method == "GET" else (json.loads(body_s) if body_s else params),
            headers=headers,
            json_body=json_body and method != "GET",
            exchange="okx",
        )

    def get_last_price(self, symbol: str) -> float:
        d = self._req("GET", "/api/v5/market/ticker", {"instId": self.mapper.native(symbol)}) or {}
        rows = d.get("data") or []
        return float((rows[0] if rows else {}).get("last") or 0)

    def get_ticker(self, symbol: str) -> dict:
        native = self.mapper.native(symbol)
        d = self._req("GET", "/api/v5/market/ticker", {"instId": native}) or {}
        row = (d.get("data") or [{}])[0]
        f = self._req("GET", "/api/v5/public/funding-rate", {"instId": native}) or {}
        frow = (f.get("data") or [{}])[0]
        return {
            "last": _f(row.get("last")),
            "mark_price": _f((self._req("GET", "/api/v5/public/mark-price", {"instId": native}) or {"data": [{}]})["data"][0].get("markPx")),
            "index_price": _f(row.get("indexPx") or (self._req("GET", "/api/v5/public/mark-price", {"instId": native}) or {"data": [{}]})["data"][0].get("idxPx")),
            "funding_rate": _f(frow.get("fundingRate")),
            "high_24h": _f(row.get("high24h")),
            "low_24h": _f(row.get("low24h")),
            "change_percentage": None,
            "volume_24h_quote": _f(row.get("volCcy24h")),
            "highest_bid": _f(row.get("bidPx")),
            "lowest_ask": _f(row.get("askPx")),
        }

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        bar = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D"}.get(interval, "15m")
        rows = (self._req("GET", "/api/v5/market/candles", {
            "instId": self.mapper.native(symbol), "bar": bar, "limit": min(limit, 300),
        }) or {}).get("data") or []
        out = []
        for r in rows:
            out.append({
                "t": int(r[0]) // 1000,
                "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                "v": float(r[5]), "sum": float(r[7]) if r[7] else 0.0,
                "ema20": None, "atr14": None,
            })
        return list(reversed(out))

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        d = self._req("GET", "/api/v5/market/books", {"instId": self.mapper.native(symbol), "sz": limit}) or {}
        row = (d.get("data") or [{}])[0]
        return {
            "bids": [{"p": _f(x[0]), "s": _f(x[1])} for x in (row.get("bids") or [])],
            "asks": [{"p": _f(x[0]), "s": _f(x[1])} for x in (row.get("asks") or [])],
        }

    def get_contract(self, symbol: str) -> ContractMeta:
        d = self._req("GET", "/api/v5/public/instruments", {"instType": "SWAP", "instId": self.mapper.native(symbol)}) or {}
        row = (d.get("data") or [{}])[0]
        return ContractMeta(
            name=symbol,
            quanto_multiplier=float(_f(row.get("ctVal")) or 1.0),
            order_size_round=float(_f(row.get("lotSz")) or 1),
            order_price_round=float(_f(row.get("tickSz")) or 0.1),
            leverage_max=int(_f(row.get("lever")) or 100),
        )

    def get_account(self) -> dict:
        d = self._req("GET", "/api/v5/account/balance", {"ccy": "USDT"}, signed=True) or {}
        row = (d.get("data") or [{}])[0]
        det = (row.get("details") or [{}])[0]
        return {
            "available": det.get("availBal") or det.get("cashBal"),
            "total": det.get("eq") or det.get("cashBal"),
            "position_mode": "dual",
        }

    def get_positions(self) -> list:
        d = self._req("GET", "/api/v5/account/positions", {"instType": "SWAP"}, signed=True) or {}
        out = []
        for p in d.get("data") or []:
            pos = int(float(p.get("pos") or 0))
            if pos == 0:
                continue
            out.append({
                "contract": self.mapper.internal(p.get("instId") or ""),
                "size": pos,
                "mode": "dual_long" if pos > 0 else "dual_short",
                "entry_price": _f(p.get("avgPx")),
                "leverage": _f(p.get("lever")),
                "unrealised_pnl": _f(p.get("upl")),
                "margin": _f(p.get("margin")),
            })
        return out

    def set_leverage(self, symbol: str, leverage: int):
        return self._req("POST", "/api/v5/account/set-leverage", {
            "instId": self.mapper.native(symbol), "lever": str(int(leverage)), "mgnMode": "cross",
        }, signed=True)

    def set_margin_mode(self, symbol: str, mode: str):
        # OKX: margin mode is per-position tdMode; expose no-op for unified API
        return {"ok": True, "note": "okx tdMode set per order (cross/isolated)"}

    def is_dual_position_mode(self) -> bool:
        return True

    def place_order(self, body: dict) -> dict:
        native = self.mapper.native(body.get("contract") or body.get("symbol") or "")
        size = int(body.get("size") or 0)
        tif = (body.get("tif") or "gtc").lower()
        tif_map = {"gtc": "post_only" if tif == "poc" else "gtc", "ioc": "ioc", "fok": "fok", "poc": "post_only"}
        params = {
            "instId": native,
            "tdMode": "cross",
            "side": "buy" if size > 0 else "sell",
            "ordType": tif_map.get(tif, "gtc") if str(body.get("price")) not in ("0", "0.0", "", "None") else "market",
            "sz": str(abs(size)),
        }
        if str(body.get("price")) not in ("0", "0.0", "", "None"):
            params["px"] = str(body.get("price"))
        if body.get("reduce_only"):
            params["reduceOnly"] = "true"
        d = self._req("POST", "/api/v5/trade/order", params, signed=True) or {}
        row = (d.get("data") or [{}])[0]
        return {"id": row.get("ordId"), "order": row}

    def place_price_order(self, body: dict) -> dict:
        native = self.mapper.native(body.get("contract") or body.get("symbol") or "")
        trig = body.get("trigger") or {}
        ini = body.get("initial") or {}
        size = int(ini.get("size") or 0)
        side = "buy" if size > 0 else "sell"
        params = {
            "instId": native, "tdMode": "cross", "side": side,
            "ordType": "conditional", "sz": str(abs(size)),
            "slTriggerPx": str(trig.get("price")), "slOrdPx": "-1",
        }
        d = self._req("POST", "/api/v5/trade/order", params, signed=True) or {}
        row = (d.get("data") or [{}])[0]
        return {"id": row.get("ordId"), "raw": row}

    def get_order(self, order_id: str) -> dict:
        return self._req("GET", "/api/v5/trade/order", {"ordId": order_id}, signed=True) or {}

    def get_price_order(self, price_order_id: str) -> dict:
        return self.get_order(price_order_id)

    def list_orders(self, contract: Optional[str] = None):
        params: dict = {"instType": "SWAP", "state": "live"}
        if contract:
            params["instId"] = self.mapper.native(contract)
        d = self._req("GET", "/api/v5/trade/orders-pending", params, signed=True) or {}
        out = []
        for o in d.get("data") or []:
            sz = int(float(o.get("sz") or 0))
            out.append({
                "id": o.get("ordId"),
                "text": o.get("clOrdId"),
                "contract": self.mapper.internal(o.get("instId") or ""),
                "size": sz if o.get("side") == "buy" else -sz,
                "price": o.get("px"),
                "left": sz - int(float(o.get("accFillSz") or 0)),
                "status": o.get("state"),
            })
        return out

    def list_price_orders(self, contract: Optional[str] = None):
        return self.list_orders(contract)

    def cancel_order(self, order_id: str):
        return self._req("POST", "/api/v5/trade/cancel-order", {"ordId": order_id}, signed=True)

    def cancel_all_orders(self, contract: str):
        return self._req("POST", "/api/v5/trade/cancel-all", {"instId": self.mapper.native(contract)}, signed=True)

    def cancel_price_order(self, order_id: str):
        return self.cancel_order(order_id)

    def cancel_all_price_orders(self, contract: Optional[str] = None):
        return self.cancel_all_orders(contract or "")

    def close_position(self, contract: str, side: Optional[str] = None, size: int = 0):
        pos = [p for p in self.get_positions() if p.get("contract") == contract]
        amt = sum(int(p.get("size") or 0) for p in pos)
        if not amt:
            return {"closed": 0}
        return self.place_order({"contract": contract, "size": -amt, "price": 0, "tif": "ioc", "reduce_only": True})


class OkxMapper(SymbolMapper):
    def native(self, symbol: str) -> str:
        s = symbol.replace("_", "-")
        if not s.endswith("-SWAP"):
            s = s + "-SWAP"
        return s

    def internal(self, native: str) -> str:
        s = str(native).replace("-SWAP", "")
        if "-" in s and "_" not in s:
            s = s.replace("-", "_")
        return s
