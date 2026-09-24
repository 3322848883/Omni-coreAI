"""Gate.io Futures REST client (APIv4 signing + account/contract helpers)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

LIVE_REST = "https://api.gateio.ws"
TESTNET_REST = "https://api-testnet.gateapi.io"
FUTURES_API = "/api/v4/futures/usdt"

SYMBOL_MAP = {
    "BTC": "BTC_USDT",
    "ETH": "ETH_USDT",
    "SOL": "SOL_USDT",
    "XAU": "XAU_USDT",
    "XAG": "XAG_USDT",
    "AU": "XAU_USDT",
    "AG": "XAG_USDT",
}


class GateApiError(Exception):
    def __init__(self, message: str, status: int = 0, label: str = ""):
        super().__init__(message)
        self.status = status
        self.label = label


@dataclass
class ContractMeta:
    name: str
    quanto_multiplier: float
    order_size_round: float
    order_price_round: float
    leverage_max: int


@dataclass
class GateClient:
    api_key: str
    api_secret: str
    env: str = "live"  # live | testnet
    base: str = ""
    timeout: int = 15
    _contract_cache: dict = field(default_factory=dict)
    _contract_cache_ts: float = 0.0
    _position_mode_cache: tuple = field(default_factory=lambda: (None, 0.0))

    def __post_init__(self):
        if self.env not in ("live", "testnet"):
            raise ValueError(f"env must be live|testnet, got {self.env!r}")
        if not self.base:
            self.base = TESTNET_REST if self.env == "testnet" else LIVE_REST

    def banner(self) -> str:
        label = "模拟盘 TESTNET" if self.env == "testnet" else "实盘 LIVE"
        return f"[{label}] {self.base}"

    # ── signing ──────────────────────────────────────────
    def rest_signed_request(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: Any = None,
    ) -> Any:
        url = f"{self.base}{path}"
        if query_string:
            url += f"?{query_string}"

        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        body_hash = hashlib.sha512(body_str.encode("utf8")).hexdigest()
        timestamp = str(int(time.time()))
        sign_str = f"{method}\n{path}\n{query_string}\n{body_hash}\n{timestamp}"
        sign = hmac.new(
            self.api_secret.encode("utf8"), sign_str.encode("utf8"), hashlib.sha512
        ).hexdigest()
        headers = {
            "KEY": self.api_key,
            "SIGN": sign,
            "Timestamp": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "gate-signal-bot/0.1",
        }
        data = body_str.encode("utf8") if body_str else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf8")
            except Exception:
                pass
            label = ""
            try:
                label = (json.loads(err_body) or {}).get("label", "")
            except Exception:
                pass
            msg = label or err_body or str(e)
            raise GateApiError(f"{e.code} {path}: {msg}", status=e.code, label=label) from e
        except Exception as e:
            raise GateApiError(f"request failed {path}: {e}") from e

    def public_get(self, path: str, query_string: str = "") -> Any:
        url = f"{self.base}{path}"
        if query_string:
            url += f"?{query_string}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf8")
                return json.loads(raw) if raw.strip() else {}
        except Exception as e:
            raise GateApiError(f"public get failed {path}: {e}") from e

    # ── account / market ─────────────────────────────────
    def get_account(self) -> dict:
        # Official: GET /futures/usdt/accounts (plural). /account is 404.
        return self.rest_signed_request("GET", f"{FUTURES_API}/accounts") or {}

    def get_position_mode(self) -> str:
        cached, ts = self._position_mode_cache
        if cached and (time.time() - ts) < 30:
            return cached
        account = self.get_account() or {}
        mode = str(account.get("position_mode") or "single")
        if account.get("in_dual_mode") or mode == "dual":
            mode = "dual"
        self._position_mode_cache = (mode, time.time())
        return mode

    def is_dual_position_mode(self) -> bool:
        return self.get_position_mode() in ("dual", "dual_long_short", "dual_plus")

    def get_positions(self) -> list:
        result = self.rest_signed_request("GET", f"{FUTURES_API}/positions")
        return result or []

    def get_contracts(self, max_age_sec: int = 3600) -> dict[str, ContractMeta]:
        now = time.time()
        if self._contract_cache and (now - self._contract_cache_ts) < max_age_sec:
            return self._contract_cache
        raw = self.public_get(f"{FUTURES_API}/contracts") or []
        cache: dict[str, ContractMeta] = {}
        for item in raw:
            name = str(item.get("name") or "")
            if not name:
                continue
            cache[name] = ContractMeta(
                name=name,
                quanto_multiplier=float(item.get("quanto_multiplier") or 1),
                order_size_round=float(item.get("order_size_round") or 1),
                order_price_round=float(item.get("order_price_round") or 0.1),
                leverage_max=int(item.get("leverage_max") or 100),
            )
        self._contract_cache = cache
        self._contract_cache_ts = now
        return cache

    def get_contract(self, symbol: str) -> ContractMeta:
        contracts = self.get_contracts()
        if symbol not in contracts:
            raise GateApiError(f"unknown contract: {symbol}")
        return contracts[symbol]

    def get_ticker(self, symbol: str) -> dict:
        raw = self.public_get(f"{FUTURES_API}/tickers", f"contract={symbol}")
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        if not isinstance(raw, dict) or raw.get("last") is None:
            raise GateApiError(f"no ticker for {symbol}")
        return raw

    def get_last_price(self, symbol: str) -> float:
        return float(self.get_ticker(symbol).get("last"))

    def get_contract_stats(self, symbol: str, limit: int = 1) -> list:
        raw = self.public_get(
            f"{FUTURES_API}/contract_stats", f"contract={symbol}&limit={int(limit)}"
        )
        return list(raw or [])

    def get_orderbook_top(self, symbol: str, limit: int = 5) -> dict:
        raw = self.public_get(f"{FUTURES_API}/order_book", f"contract={symbol}&limit={int(limit)}") or {}
        return {
            "bids": raw.get("bids") or [],
            "asks": raw.get("asks") or [],
            "current": raw.get("current"),
        }

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        # Official: leverage is a QUERY param; dual uses dual_comp path.
        qs = f"leverage={int(leverage)}"
        if self.is_dual_position_mode():
            path = f"{FUTURES_API}/dual_comp/positions/{symbol}/leverage"
        else:
            path = f"{FUTURES_API}/positions/{symbol}/leverage"
        return self.rest_signed_request("POST", path, qs, None)

    def set_margin_mode(self, symbol: str, margin_mode: str) -> Any:
        # Official FuturesPositionCrossMode: mode = ISOLATED | CROSS (uppercase)
        mode = "ISOLATED" if str(margin_mode).lower() == "isolated" else "CROSS"
        body = {"contract": symbol, "mode": mode}
        path = f"{FUTURES_API}/positions/cross_mode"
        if self.is_dual_position_mode():
            path = f"{FUTURES_API}/dual_comp/positions/cross_mode"
        return self.rest_signed_request("POST", path, "", body)

    # ── orders ───────────────────────────────────────────
    def place_order(self, body: dict) -> dict:
        try:
            result = self.rest_signed_request("POST", f"{FUTURES_API}/orders", "", body)
        except GateApiError as e:
            # market slippage: fall back once to near-touch limit
            if e.label == "MARKET_PRICE_TOO_DEVIATED" and str(body.get("price", "0")) == "0":
                retry = dict(body)
                contract = str(body.get("contract") or "")
                side_buy = int(body.get("size") or 0) > 0
                ob = self.public_get(f"{FUTURES_API}/order_book", f"contract={contract}&limit=1")
                levels = (ob.get("bids") if side_buy else ob.get("asks")) or []
                px = str((levels[0] if levels else {}).get("p") or "")
                if not px:
                    raise
                retry["price"] = px
                retry["tif"] = "gtc"
                result = self.rest_signed_request("POST", f"{FUTURES_API}/orders", "", retry)
            else:
                raise
        if not isinstance(result, dict):
            raise GateApiError(f"unexpected place_order response: {result!r}")
        return result

    def place_price_order(self, body: dict) -> dict:
        result = self.rest_signed_request("POST", f"{FUTURES_API}/price_orders", "", body)
        if not isinstance(result, dict):
            raise GateApiError(f"unexpected place_price_order response: {result!r}")
        return result

    def get_order(self, order_id: str) -> dict:
        """GET one order (any status) — used to confirm it actually landed."""
        return self.rest_signed_request("GET", f"{FUTURES_API}/orders/{order_id}", "") or {}

    def get_price_order(self, price_order_id: str) -> dict:
        return self.rest_signed_request("GET", f"{FUTURES_API}/price_orders/{price_order_id}", "") or {}

    def list_orders(self, contract: Optional[str] = None) -> list:
        qs = "status=open"
        if contract:
            qs += f"&contract={contract}"
        return self.rest_signed_request("GET", f"{FUTURES_API}/orders", qs) or []

    def list_price_orders(self, contract: Optional[str] = None) -> list:
        qs = "status=open"
        if contract:
            qs += f"&contract={contract}"
        return self.rest_signed_request("GET", f"{FUTURES_API}/price_orders", qs) or []

    def cancel_order(self, order_id: str) -> Any:
        return self.rest_signed_request("DELETE", f"{FUTURES_API}/orders/{order_id}")

    def cancel_all_orders(self, contract: str) -> Any:
        return self.rest_signed_request(
            "DELETE", f"{FUTURES_API}/orders", f"contract={contract}"
        )

    def cancel_price_order(self, order_id: str) -> Any:
        return self.rest_signed_request("DELETE", f"{FUTURES_API}/price_orders/{order_id}")

    def cancel_all_price_orders(self, contract: Optional[str] = None) -> Any:
        qs = f"contract={contract}" if contract else ""
        return self.rest_signed_request("DELETE", f"{FUTURES_API}/price_orders", qs)

    def get_available_usdt(self) -> float:
        account = self.get_account() or {}
        return float(account.get("available") or 0)

    def stop_trailing_orders(self, contract: Optional[str] = None) -> Any:
        qs = f"contract={contract}" if contract else ""
        return self.rest_signed_request("DELETE", f"{FUTURES_API}/autoorder/v1/trail", qs)

    def place_trailing_order(self, body: dict) -> dict:
        """POST /autoorder/v1/trail/create — Gate trailing stop."""
        result = self.rest_signed_request(
            "POST", f"{FUTURES_API}/autoorder/v1/trail/create", "", body
        )
        if not isinstance(result, dict):
            raise GateApiError(f"unexpected place_trailing_order response: {result!r}")
        # HTTP 200 but business failure: {code:-1, message:...}
        if result.get("code") not in (None, 0, "0"):
            raise GateApiError(
                f"trail rejected: {result.get('message') or result}",
                label=str(result.get("code")),
            )
        return result

    def close_position(self, contract: str, side: Optional[str] = None, size: int = 0) -> dict:
        """Official close (verified on testnet).

        single full close: size=0, close=true, reduce_only=true
        dual reduce:       reduce_only=true, size>0 reduces short, size<0 reduces long
        """
        if size < 0:
            raise GateApiError("close size must be >= 0")
        dual = self.is_dual_position_mode()
        if dual and side not in ("long", "short"):
            raise GateApiError("dual position mode requires side=long|short for close")

        if not dual and size == 0:
            try:
                return self.place_order({
                    "contract": contract,
                    "size": 0,
                    "close": True,
                    "price": "0",
                    "tif": "ioc",
                    "reduce_only": True,
                })
            except GateApiError as e:
                # flatten/close_all on an already-flat book is a no-op success
                if _is_empty_position_error(e):
                    return {"id": None, "status": "already_flat", "contract": contract}
                raise

        positions = self.get_positions()
        target = [
            p for p in positions
            if p.get("contract") == contract and int(p.get("size") or 0) != 0
        ]
        if side:
            target = [p for p in target if _pos_is_side(p, side)]
        if not target:
            raise GateApiError(f"no position to close on {contract} side={side}")
        if not side:
            side = _infer_side(target[0])
        total = sum(abs(int(p.get("size") or 0)) for p in target)
        close_size = total if size == 0 else min(size, total)
        # sell to close long (negative), buy to close short (positive)
        order_size = -abs(close_size) if side == "long" else abs(close_size)
        return self.place_order({
            "contract": contract,
            "size": order_size,
            "price": "0",
            "tif": "ioc",
            "reduce_only": True,
        })


def _is_empty_position_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "position_empty" in msg or "no position" in msg


def _pos_is_side(pos: dict, side: str) -> bool:
    size = int(pos.get("size") or 0)
    mode = str(pos.get("mode") or "").lower()
    if side == "long":
        return size > 0 or mode.endswith("long")
    return size < 0 or mode.endswith("short")


def _infer_side(pos: dict) -> str:
    return "long" if _pos_is_side(pos, "long") else "short"


def resolve_symbol(name: str) -> str:
    upper = (name or "").upper().strip()
    if not upper:
        raise GateApiError("symbol is required")
    if upper in SYMBOL_MAP:
        return SYMBOL_MAP[upper]
    if "_" in upper:
        base = upper.split("_")[0]
        return SYMBOL_MAP.get(base, upper)
    return SYMBOL_MAP.get(upper, f"{upper}_USDT")


def load_credentials(env: str, api_key_env: str = "", api_secret_env: str = "") -> tuple[str, str]:
    if env == "testnet":
        key_env = api_key_env or "GATE_TESTNET_API_KEY"
        secret_env = api_secret_env or "GATE_TESTNET_API_SECRET"
    else:
        key_env = api_key_env or "GATE_API_KEY"
        secret_env = api_secret_env or "GATE_API_SECRET"
    key = os.environ.get(key_env, "").strip()
    secret = os.environ.get(secret_env, "").strip()
    if not key or not secret:
        raise GateApiError(
            f"missing credentials: set {key_env} and {secret_env} for env={env}"
        )
    return key, secret
