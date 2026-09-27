"""Binance USDT-M futures adapter (market + trading)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .base import ExchangeClient, ExchangeError, SymbolMapper

# intervals shared with gate_bot.strategist.market
INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


class BinanceExchange(ExchangeClient):
    name = "binance"
    supports_testnet = True
    supports_price_orders = True
    supports_margin_mode = True

    def __init__(self, env: str = "live", api_key: str = "", api_secret: str = "", **kwargs: Any):
        super().__init__(env=env, api_key=api_key, api_secret=api_secret, **kwargs)
        if self.env == "testnet":
            self.base = "https://testnet.binancefuture.com"
        else:
            self.base = str(kwargs.get("base_url") or "https://fapi.binance.com").rstrip("/")
        self.mapper = SymbolMapper.strip_quote("binance", "USDT")

    # ── http ──────────────────────────────────────────
    def _req(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False) -> Any:
        params = dict(params or {})
        qs = urllib.parse.urlencode(params)
        url = f"{self.base}{path}"
        headers = {"User-Agent": "gate-signal-bot"}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            qs = urllib.parse.urlencode(params)
            mac = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            qs = qs + "&signature=" + mac
            headers["X-MBX-APIKEY"] = self.api_key
            method = "GET" if method == "GET" else method
            if method == "GET":
                req = urllib.request.Request(f"{url}?{qs}", headers=headers, method="GET")
            else:
                req = urllib.request.Request(url, data=qs.encode(), headers={**headers, "Content-Type": "application/x-www-form-urlencoded"}, method=method)
        else:
            if method == "GET":
                req = urllib.request.Request(f"{url}?{qs}" if qs else url, headers=headers, method="GET")
            else:
                req = urllib.request.Request(url, data=json.dumps(params).encode(),
                                             headers={**headers, "Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "replace")[:300]
            raise ExchangeError(f"binance {e.code}: {err}", status=e.code, exchange="binance") from e
        except Exception as e:  # noqa: BLE001
            raise ExchangeError(f"binance request failed: {e}", exchange="binance") from e
        try:
            return json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            return {"raw": raw}

    def _pub(self, path: str, params: Optional[dict] = None) -> Any:
        return self._req("GET", path, params, signed=False)

    def _sig(self, method: str, path: str, params: Optional[dict] = None) -> Any:
        return self._req(method, path, params, signed=True)

    # ── market ────────────────────────────────────────
    def get_last_price(self, symbol: str) -> float:
        d = self._pub("/fapi/v1/ticker/price", {"symbol": self.mapper.native(symbol)})
        return float(d.get("price") or 0)

    def get_ticker(self, symbol: str) -> dict:
        sym = self.mapper.native(symbol)
        d = self._pub("/fapi/v1/ticker/24hr", {"symbol": sym}) or {}
        mark = self._pub("/fapi/v1/premiumIndex", {"symbol": sym}) or {}
        return {
            "last": _f(d.get("lastPrice")),
            "mark_price": _f(mark.get("markPrice")),
            "index_price": _f(mark.get("indexPrice")),
            "funding_rate": _f(mark.get("lastFundingRate")),
            "high_24h": _f(d.get("highPrice")),
            "low_24h": _f(d.get("lowPrice")),
            "change_percentage": _f(d.get("priceChangePercent")),
            "change_price": _f(d.get("priceChange")),
            "volume_24h_quote": _f(d.get("quoteVolume")),
            "highest_bid": _f(d.get("bidPrice")),
            "lowest_ask": _f(d.get("askPrice")),
            "total_size": _f(d.get("volume")),
        }

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        rows = self._pub("/fapi/v1/klines", {
            "symbol": self.mapper.native(symbol),
            "interval": interval,
            "limit": min(limit, 1500),
        }) or []
        out = []
        for r in rows:
            out.append({
                "t": int(r[0]) // 1000,
                "o": float(r[1]),
                "h": float(r[2]),
                "l": float(r[3]),
                "c": float(r[4]),
                "v": float(r[5]),
                "sum": float(r[7]) if r[7] else 0.0,
                "ema20": None,
                "atr14": None,
            })
        return out

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        # Binance depth limit must be one of 5/10/20/50/100/500/1000
        allowed = (5, 10, 20, 50, 100, 500, 1000)
        lim = next((x for x in allowed if x >= max(1, int(limit))), 5)
        d = self._pub("/fapi/v1/depth", {"symbol": self.mapper.native(symbol), "limit": lim}) or {}
        n = max(1, int(limit))
        return {
            "bids": [{"p": _f(x[0]), "s": _f(x[1])} for x in (d.get("bids") or [])][:n],
            "asks": [{"p": _f(x[0]), "s": _f(x[1])} for x in (d.get("asks") or [])][:n],
        }

    def get_contract(self, symbol: str):
        from ..gate_client import ContractMeta

        sym = self.mapper.native(symbol)
        rows = self._pub("/fapi/v1/exchangeInfo", {}) or {}
        info = next((x for x in rows.get("symbols") or [] if x.get("symbol") == sym), {}) or {}
        fl = next((x for x in info.get("filters") or [] if x.get("filterType") == "LOT_SIZE"), {}) or {}
        pf = next((x for x in info.get("filters") or [] if x.get("filterType") == "PRICE_FILTER"), {}) or {}
        quanto = 1.0
        if info.get("contractType") == "PERPETUAL" and info.get("underlyingType") == "COIN":
            quanto = _f(info.get("underlyingMultiplier")) or 1.0
        # USDT-M: 1 contract = 1 base unit typically (multiplier 1)
        try:
            pos = self._sig("GET", "/fapi/v2/leverageBracket", {"symbol": sym}) or []
            lev_max = int((pos[0].get("brackets") or [{}])[0].get("initialLeverage") or 100)
        except Exception:  # noqa: BLE001
            lev_max = 100
        return ContractMeta(
            name=symbol,
            quanto_multiplier=float(quanto),
            order_size_round=float(fl.get("stepSize") or 1),
            order_price_round=float(pf.get("tickSize") or 0.1),
            leverage_max=lev_max,
        )

    def get_contract_stats(self, symbol: str, limit: int = 1) -> list:
        d = self._pub("/fapi/v1/openInterest", {"symbol": self.mapper.native(symbol)}) or {}
        return [{
            "open_interest": _f(d.get("openInterest")),
            "open_interest_usd": None,
            "mark_price": None,
        }]

    # ── account ───────────────────────────────────────
    def get_account(self) -> dict:
        d = self._sig("GET", "/fapi/v2/balance") or []
        row = next((x for x in d if x.get("asset") in ("USDT", "USDT")), {})
        return {
            "available": row.get("availableBalance") or row.get("balance"),
            "total": row.get("balance"),
            "position_mode": "dual" if self.is_dual_position_mode() else "single",
        }

    def get_positions(self) -> list:
        rows = self._sig("GET", "/fapi/v2/positionRisk") or []
        out = []
        for p in rows:
            if int(float(p.get("positionAmt") or 0)) == 0:
                continue
            out.append({
                "contract": self.mapper.internal(p.get("symbol") or ""),
                "size": int(float(p.get("positionAmt") or 0)),
                "mode": "dual_long" if float(p.get("positionAmt") or 0) > 0 else "dual_short",
                "entry_price": _f(p.get("entryPrice")),
                "leverage": _f(p.get("leverage")),
                "unrealised_pnl": _f(p.get("unRealizedProfit")),
                "liq_price": _f(p.get("liquidationPrice")),
                "margin": _f(p.get("isolatedMargin")),
            })
        return out

    def set_leverage(self, symbol: str, leverage: int):
        return self._sig("POST", "/fapi/v1/leverage", {"symbol": self.mapper.native(symbol), "leverage": int(leverage)})

    def set_margin_mode(self, symbol: str, mode: str):
        # ISOLATED | CROSSED
        m = "ISOLATED" if str(mode).lower().startswith("iso") else "CROSSED"
        return self._sig("POST", "/fapi/v1/marginType", {"symbol": self.mapper.native(symbol), "marginType": m})

    def is_dual_position_mode(self) -> bool:
        try:
            d = self._sig("GET", "/fapi/v1/positionSide/dual") or {}
            return bool(d.get("dualSidePosition"))
        except Exception:  # noqa: BLE001
            return False

    def get_position_mode(self) -> str:
        return "dual" if self.is_dual_position_mode() else "single"

    # ── trading ───────────────────────────────────────
    def place_order(self, body: dict) -> dict:
        """body: contract|symbol, size(+/-), price, tif (gtc/ioc/poc?) / type."""
        sym = self.mapper.native(body.get("contract") or body.get("symbol") or "")
        size = int(body.get("size") or 0)
        params = {
            "symbol": sym,
            "side": "BUY" if size > 0 else "SELL",
            "positionSide": "BOTH",
            "quantity": str(abs(size)),
        }
        tif = (body.get("tif") or "").upper()
        price = body.get("price")
        if tif == "IOC" or body.get("reduce_only") and False:
            params["type"] = "MARKET" if str(price) in ("0", "0.0", "") else "LIMIT"
        if str(price) in ("0", "0.0", None):
            params["type"] = "MARKET"
        else:
            params["type"] = "LIMIT"
            params["price"] = str(price)
            params["timeInForce"] = tif if tif in ("GTC", "IOC", "FOK", "GTX") else "GTC"
        if body.get("reduce_only"):
            params["reduceOnly"] = "true"
        if body.get("close"):
            params["closePosition"] = "true"
        d = self._sig("POST", "/fapi/v1/order", params) or {}
        return {"order": d, "id": d.get("orderId")}

    def place_price_order(self, body: dict) -> dict:
        """TP/SL: Gate style {contract,size,initial,trigger} -> Binance STOP_MARKET."""
        sym = self.mapper.native(body.get("contract") or body.get("symbol") or "")
        trig = body.get("trigger") or {}
        ini = body.get("initial") or {}
        size = int(ini.get("size") or body.get("size") or 0)
        trigger_price = trig.get("price") or body.get("trigger_price")
        rule = int(trig.get("rule") or 0)
        # rule 1 = price above (TP short / SL long), rule 2 = price below
        side = "BUY" if size > 0 else "SELL"
        params = {
            "symbol": sym,
            "side": side,
            "positionSide": "BOTH",
            "type": "STOP_MARKET",
            "stopPrice": str(trigger_price),
            "closePosition": "true",
        }
        d = self._sig("POST", "/fapi/v1/order", params) or {}
        return {"id": d.get("orderId"), "raw": d}

    def get_order(self, order_id: str) -> dict:
        return self._sig("GET", "/fapi/v1/order", {"orderId": order_id}) or {}

    def get_price_order(self, price_order_id: str) -> dict:
        return self.get_order(price_order_id)

    def list_orders(self, contract: Optional[str] = None):
        params = {}
        if contract:
            params["symbol"] = self.mapper.native(contract)
        rows = self._sig("GET", "/fapi/v1/openOrders", params) or []
        out = []
        for o in rows:
            out.append({
                "id": o.get("orderId"),
                "text": o.get("clientOrderId"),
                "contract": self.mapper.internal(o.get("symbol") or ""),
                "size": int(float(o.get("origQty") or 0)) * (1 if o.get("side") == "BUY" else -1),
                "price": o.get("price"),
                "left": int(float(o.get("origQty") or 0) - float(o.get("executedQty") or 0)),
                "status": o.get("status"),
                "tif": o.get("timeInForce"),
            })
        return out

    def list_price_orders(self, contract: Optional[str] = None):
        return self.list_orders(contract)

    def cancel_order(self, order_id: str):
        return self._sig("DELETE", "/fapi/v1/order", {"orderId": order_id})

    def cancel_all_orders(self, contract: str):
        return self._sig("DELETE", "/fapi/v1/allOpenOrders", {"symbol": self.mapper.native(contract)})

    def cancel_price_order(self, order_id: str):
        return self.cancel_order(order_id)

    def cancel_all_price_orders(self, contract: Optional[str] = None):
        if not contract:
            return self.cancel_all_orders("")
        return self.cancel_all_orders(contract)

    def close_position(self, contract: str, side: Optional[str] = None, size: int = 0):
        pos = [p for p in self.get_positions() if p.get("contract") == contract]
        amt = sum(int(p.get("size") or 0) for p in pos)
        if amt == 0:
            return {"closed": 0}
        side_s = "SELL" if amt > 0 else "BUY"
        return self.place_order({
            "contract": contract,
            "size": -amt,
            "price": 0,
            "tif": "IOC",
            "reduce_only": True,
        })
