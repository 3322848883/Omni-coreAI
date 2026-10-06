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
            "User-Agent": "OmniAlpha/0.1",
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

    def public_get(self, path: str, query_string: str = "", attempts: int = 3) -> Any:
        """GET 公开端点。**对瞬时网络故障做有界重试**。

        本地直连交易所 API 的链路会抖（实测底层错误全是 TLS 层：
        `handshake operation timed out` / `SSL: UNEXPECTED_EOF_WHILE_READING` /
        `IncompleteRead(1168689 bytes read, 137723 more expected)`），而原来这里
        一次失败就抛 —— 于是每次抖动都变成一笔操作失败（模拟盘 20% 的失败源于此）。
        """
        url = f"{self.base}{path}"
        if query_string:
            url += f"?{query_string}"
        last: Optional[Exception] = None
        for i in range(max(1, int(attempts))):
            req = urllib.request.Request(url, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf8")
                    return json.loads(raw) if raw.strip() else {}
            except Exception as e:  # noqa: BLE001 — 只重试瞬时故障，其余立即抛
                last = e
                if i + 1 >= max(1, int(attempts)) or not self._is_transient(e):
                    break
                time.sleep(0.4 * (i + 1))
        raise GateApiError(f"public get failed {path}: {last}") from last

    @staticmethod
    def _is_transient(e: Exception) -> bool:
        """瞬时网络/传输故障（值得重试）；4xx 之类不该重试。"""
        s = str(e).lower()
        return any(h in s for h in (
            "timed out", "timeout", "eof occurred", "unexpected_eof", "connection reset",
            "incompleteread", "handshake", "remote end closed", "connection aborted",
            "temporarily unavailable", "tunnel connection failed",
        ))

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
        try:
            raw = self.public_get(f"{FUTURES_API}/contracts") or []
        except GateApiError:
            # 合约元数据（quanto_multiplier / tick / lot / 杠杆上限）几乎不变，所以拉取失败时
            # **回退到上一份缓存**，而不是让整笔操作失败。实测 /contracts 返回约 1.1 MB，
            # 在抖动的 TLS 链路上极易截断（IncompleteRead / handshake timeout）——
            # 模拟盘「public get failed」有 93%（95/102）来自这个端点。
            # 首次调用（无缓存）时仍然抛出，不掩盖真正的不可用。
            if self._contract_cache:
                return self._contract_cache
            raise
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

    def get_contract_stats(self, symbol: str, limit: int = 1, interval: str = "") -> list:
        """合约市场结构统计。`interval` 留空时用交易所默认粒度。

        显式传 `interval` 可拿**按周期聚合的历史序列**（Gate 支持
        1m/5m/15m/30m/1h/4h/8h/1d，limit 实测可到 2000）——持仓量类指标
        （OI 四象限、Delta、Money Flow）需要序列而非单点快照。
        """
        qs = f"contract={symbol}&limit={int(limit)}"
        if interval:
            qs += f"&interval={interval}"
        raw = self.public_get(f"{FUTURES_API}/contract_stats", qs)
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
            # market slippage: fall back once to a taker limit (cross the book, IOC)
            # Gate may label the reject MARKET_PRICE_TOO_DEVIATED or PRICE_TOO_DEVIATED
            if (
                e.label in ("MARKET_PRICE_TOO_DEVIATED", "PRICE_TOO_DEVIATED")
                and str(body.get("price", "0")) == "0"
            ):
                def _num(v):
                    try:
                        return float(v) if v is not None and v != "" else None
                    except (TypeError, ValueError):
                        return None

                retry = dict(body)
                contract = str(body.get("contract") or "")
                side_buy = int(body.get("size") or 0) > 0
                # prefer last/mark as fair value; clamp book top so we never chase a stale wide book
                ref = None
                try:
                    t = self.get_ticker(contract) or {}
                    ref = _num(t.get("last")) or _num(t.get("mark_price"))
                except Exception:  # noqa: BLE001
                    ref = None
                ob = self.public_get(f"{FUTURES_API}/order_book", f"contract={contract}&limit=1")
                levels = (ob.get("asks") if side_buy else ob.get("bids")) or []
                book_px = _num((levels[0] if levels else {}).get("p"))
                if book_px is None and ref is None:
                    raise
                slip = 0.002  # 0.2% taker allowance around fair
                if ref and book_px:
                    lo, hi = ref * (1 - slip), ref * (1 + slip)
                    px_f = min(max(book_px, lo), hi)
                elif ref:
                    px_f = ref * (1 + slip if side_buy else 1 - slip)
                else:
                    px_f = book_px
                retry["price"] = str(int(px_f)) if float(px_f).is_integer() else str(px_f)
                retry["tif"] = "ioc"
                try:
                    meta = self.get_contract(contract)
                    step = float(getattr(meta, "order_price_round", 0) or 0)
                    if step > 0:
                        px_f = round(px_f / step) * step
                        retry["price"] = str(int(px_f)) if float(px_f).is_integer() else str(px_f)
                except Exception:  # noqa: BLE001 — best-effort tick align
                    pass
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

    def list_my_trades(self, contract: Optional[str] = None,
                       limit: int = 1000, last_id: Optional[str] = None) -> list:
        """已成交明细 —— 每笔带 `pnl`（该笔的**已实现盈亏**）。

        **为什么需要它**：交易所侧触发的 SL/TP 平仓**没有本地信号**，所以既不进
        `logs/trades.jsonl`、也不进 `data/shared/receipts/`。实测某 bot 的 339 行
        成交日志里只有 3 条带 `realized_pnl`、83 条执行回执里只有 4 条。要回答
        「这个 bot 到底平了几笔、赚亏多少」，只能从交易所拉 —— `paper/store.py`
        的 `realized_pnl_stats` docstring 也是这个口径。

        `limit` 上限 1000；`last_id` 用于增量（只取 id 更大的成交）。
        """
        qs = f"limit={max(1, min(int(limit), 1000))}"
        if contract:
            qs += f"&contract={contract}"
        if last_id:
            qs += f"&last_id={last_id}"
        return self.rest_signed_request("GET", f"{FUTURES_API}/my_trades", qs) or []

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
