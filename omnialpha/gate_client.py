"""Gate.io Futures REST client (APIv4 signing + account/contract helpers)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

LIVE_REST = "https://api.gateio.ws"
TESTNET_REST = "https://api-testnet.gateapi.io"
FUTURES_API = "/api/v4/futures/usdt"


def _wire_body(obj: Any) -> Any:
    """下单体 JSON 归一化：**整数值 float → int**（返回新对象，不改调用方 dict）。

    为什么必须在 wire 层做：Gate 的 `orders` / `price_orders` 里 `initial.size`
    是 **int64**（Go 后端），JSON 浮点字面量（`-8.0`）在**反序列化阶段就失败**，
    返回 400 `AUTO_INVALID_REQUEST_BODY` —— 它不是业务 label，日志里看不到原因。
    实测 2026-10-27（同 body、同持仓，只差字面量类型）：
      `"size": 0`   → `AUTO_INITIAL_SIZE_OVER_RANGE`（body 解析成功，业务层拒）
      `"size": 0.0` → `AUTO_INVALID_REQUEST_BODY`（body 没进业务层）
    4247ef8 把下单链 size 改成 float 后，martin-grid 实盘 10 小时**所有新单全灭**。

    真小数（0.7 张）必须保留小数 —— 那是 enable_decimal 合约的合法张数，
    Gate 对它的解析不受影响（只有整数值的 `8.0` 这种才多余）。
    """
    if isinstance(obj, dict):
        return {k: _wire_body(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_wire_body(v) for v in obj]
    if isinstance(obj, bool) or not isinstance(obj, float):
        return obj
    if math.isfinite(obj) and obj.is_integer():
        return int(obj)
    return obj

SYMBOL_MAP = {
    "BTC": "BTC_USDT",
    "ETH": "ETH_USDT",
    "SOL": "SOL_USDT",
    "XAU": "XAU_USDT",
    "XAG": "XAG_USDT",
    "AU": "XAU_USDT",
    "AG": "XAG_USDT",
}


def _int_price_order_size(body: Any) -> Any:
    """`/price_orders`（auto orders）的 `initial.size` 必须是**整数张**。

    与 `/orders` 不同：`/orders` 接受小数 size 并在交易所侧 floor（实测 21.4 →
    持仓 21），但**触发单端点对小数字面量直接 400** `AUTO_INVALID_REQUEST_BODY`
    （实测 2026-10-27：pa-eth-v5 入场 21.4 张挂成功，同一 21.4 张挂 TP/SL 全灭）。

    因此触发单张数向零取整到整数（= 交易所 floor 后持仓的真实张数）。
    不足 1 张 → **抛错**：挂 0 张保护单等于裸仓，比拒单危险，让调用方回滚入场。
    `close=true`（venue-computed close）语义上 size 必须为 0，跳过本检查。
    """
    if not isinstance(body, dict):
        return body
    init = body.get("initial")
    if not isinstance(init, dict) or init.get("close"):
        return body
    raw = init.get("size")
    if raw is None or isinstance(raw, bool):
        return body
    try:
        size = float(raw)
    except (TypeError, ValueError):
        return body
    whole = int(size)  # 向零取整：-21.4 → -21，21.4 → 21（与交易所 floor 一致）
    if whole == 0:
        raise GateApiError(
            f"price_orders 只接受整数张：{size:g} 张不足 1 张 —— 拒绝挂 0 张保护单"
            f"（等于裸仓）")
    out = dict(body)
    new_init = dict(init)
    new_init["size"] = whole
    out["initial"] = new_init
    return out


class GateApiError(Exception):
    def __init__(self, message: str, status: int = 0, label: str = ""):
        super().__init__(message)
        self.status = status
        self.label = label


def _f(v: Any, default: float = 0.0) -> float:
    """安全转 float。**0 与 "0" 必须保留** —— `x or default` 会把 `order_size_min=0`
    当成假值换成 1。`int("0.7")` 会抛 ValueError，所以张数一律走这里。
    """
    if v is None or v == "":
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


#: 合约磁盘缓存的 schema 版本。
#: v2（2026-10-26）：补 `size_min / size_max / enable_decimal / price_deviate /
#: maintenance_rate / maker_fee_rate / taker_fee_rate`，并把拉取端点改成带
#: `X-Gate-Size-Decimal: 1`（否则 `order_size_min` 会被 Gate 按 int 截断：0.1 → 0）。
#: v1 缓存必须丢弃 —— 否则 ETH 的 `size_step` 会从 0.1 退化成 1（缓存值盖住真实值）。
_CONTRACT_CACHE_SCHEMA = 2


@dataclass
class ContractMeta:
    name: str
    quanto_multiplier: float
    # ⚠️ 交易所**没有** `order_size_round` 这个字段（1061 个合约 0 命中，2026-10-26 实测），
    # 恒为 1 是假值。真实步长见 `size_step`。
    order_size_round: float
    order_price_round: float
    leverage_max: int
    # ── 2026-10-26 实盘挂单实测后补充 ──────────────────────
    size_min: float = 1.0          # 交易所 order_size_min：0.1（enable_decimal）或 1
    size_max: float = 0.0          # 0 = 未知/未读
    enable_decimal: bool = False   # True 时张数可到 0.1
    price_deviate: float = 0.0     # order_price_deviate（仅约束 taker 单，maker 不受限）
    maintenance_rate: float = 0.0  # 维持保证金率（ETH 0.003；paper 曾硬编码 0.01）
    maker_fee_rate: float = 0.0    # 负值 = 返佣
    taker_fee_rate: float = 0.0

    @property
    def size_step(self) -> float:
        """张数步长。实测：`enable_decimal=True` 的合约 `order_size_min=0.1`，其余 `=1`；
        且交易所对非步长倍数**静默 floor**（0.15→0.1），所以必须自己按此取整。
        """
        if self.size_min and self.size_min > 0:
            return float(self.size_min)
        return 0.1 if self.enable_decimal else 1.0


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

        # 归一化必须在**签名之前**：body_hash 与实际发送的字符串必须同源，
        # 否则 Gate 报 INVALID_SIGNATURE（见 `_wire_body` docstring 的事故）。
        if body is not None:
            body = _wire_body(body)
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

    def public_get(self, path: str, query_string: str = "", attempts: int = 3,
                   extra_headers: Optional[dict] = None) -> Any:
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
            # `extra_headers`：`X-Gate-Size-Decimal: 1` 让 Gate 把 `order_size_min`
            # 按真实小数返回（不带则 int 截断：ETH 0.1 → 0，15 个小数张合约全变 0）。
            req = urllib.request.Request(url, method="GET", headers=dict(extra_headers or {}))
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
        # 官方三态（FuturesAccount.position_mode）：single / dual / split（分仓，
        # in_dual_mode 已 deprecated）。"dual_plus" 是 set_position_mode 的请求参数名，
        # 回显是 "split" —— 两者都要算 dual；"dual_long_short" 为历史防御值保留。
        return self.get_position_mode() in (
            "dual", "dual_long_short", "dual_plus", "split")

    def get_positions(self) -> list:
        result = self.rest_signed_request("GET", f"{FUTURES_API}/positions")
        return result or []

    # ── 合约元数据磁盘缓存（跨进程）──────────────────────────
    #
    # 进程内 `_contract_cache` 只对常驻进程有效；`once` / `process` / `plan` 每次都是
    # 新进程，冷启动必拉一次 `/contracts`（约 1.37 MB），抖动链路上极易超时
    # （实测 2026-10-25：单条纸面信号 200s 跑不完；有缓存后 93s 完成）。
    def _contract_cache_file(self) -> Path:
        """按 `env + base` 区分：testnet 与 live 的合约表不同。"""
        root = Path(os.environ.get("OMNIALPHA_ROOT") or Path.cwd())
        tag = hashlib.sha1(f"{self.env}|{self.base}".encode()).hexdigest()[:8]
        return root / "data" / "cache" / f"contracts_{self.env}_{tag}.json"

    def _contract_cache_disabled(self) -> bool:
        return str(os.environ.get("OMNIALPHA_CONTRACT_CACHE") or "").strip().lower() in (
            "off", "0", "false", "no")

    def _read_contract_disk(self, max_age_sec):
        """读磁盘缓存。`max_age_sec=None` = 不看 TTL（只给「网络失败兜底」用）。"""
        if self._contract_cache_disabled():
            return {}
        try:
            f = self._contract_cache_file()
            if not f.is_file():
                return {}
            if max_age_sec is not None and (time.time() - f.stat().st_mtime) > max_age_sec:
                return {}
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("env") != self.env:
                return {}
            # 🔴 schema 闸门：v1 缓存只有 5 个字段，缺 size_min / enable_decimal。
            # 照旧读回会让 `size_step` 从 0.1 退化成 1（张数一直按整数算），
            # 所以旧版本缓存**直接判无效**，让它回源一次重写。
            if int(data.get("schema") or 1) < _CONTRACT_CACHE_SCHEMA:
                log.info("合约缓存 schema 过旧（v%s < v%s），忽略并回源",
                         data.get("schema"), _CONTRACT_CACHE_SCHEMA)
                return {}
            out: dict[str, ContractMeta] = {}
            for name, v in (data.get("contracts") or {}).items():
                if not isinstance(v, dict):
                    continue
                qm = v.get("quanto_multiplier")
                out[name] = ContractMeta(
                    name=name,
                    quanto_multiplier=float(qm) if qm is not None else None,
                    order_size_round=float(v.get("order_size_round") or 1),
                    order_price_round=float(v.get("order_price_round") or 0.1),
                    leverage_max=int(v.get("leverage_max") or 100),
                    size_min=_f(v.get("size_min"), 1.0),
                    size_max=_f(v.get("size_max"), 0.0),
                    enable_decimal=bool(v.get("enable_decimal")),
                    price_deviate=_f(v.get("price_deviate"), 0.0),
                    maintenance_rate=_f(v.get("maintenance_rate"), 0.0),
                    maker_fee_rate=_f(v.get("maker_fee_rate"), 0.0),
                    taker_fee_rate=_f(v.get("taker_fee_rate"), 0.0),
                )
            return out
        except Exception:  # noqa: BLE001 — 缓存坏了就当没有，绝不能因此让下单失败
            return {}

    def _write_contract_disk(self, cache: dict) -> None:
        """原子写（tmp + replace）。**只写成功拉到的结果**。"""
        if self._contract_cache_disabled() or not cache:
            return
        try:
            f = self._contract_cache_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "env": self.env,
                "schema": _CONTRACT_CACHE_SCHEMA,
                "saved_at": int(time.time()),
                "contracts": {k: asdict(v) for k, v in cache.items()},
            }
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, f)
        except Exception:  # noqa: BLE001
            log.debug("write contract disk cache failed", exc_info=True)

    def get_contracts(self, max_age_sec: int = 3600) -> dict[str, ContractMeta]:
        now = time.time()
        if self._contract_cache and (now - self._contract_cache_ts) < max_age_sec:
            return self._contract_cache
        # 跨进程缓存：命中就不碰网络（CLI 冷启动省掉一次 1.37 MB 请求）
        disk = self._read_contract_disk(max_age_sec)
        if disk:
            self._contract_cache, self._contract_cache_ts = disk, now
            return disk
        try:
            # 带 `X-Gate-Size-Decimal: 1`：不带的话 Gate 会把 `order_size_min` 按 int
            # 截断（ETH 0.1 → 0）。**退回分支**是给「只接受 (path, qs) 的旧子类 /
            # 测试替身」留的：真实 client 永远不会抛 TypeError，所以生产行为只有一条路。
            try:
                raw = self.public_get(
                    f"{FUTURES_API}/contracts",
                    extra_headers={"X-Gate-Size-Decimal": "1"},
                ) or []
            except TypeError:
                log.warning("public_get 不支持 extra_headers，合约步长可能被按 int 截断")
                raw = self.public_get(f"{FUTURES_API}/contracts") or []
        except GateApiError:
            # 合约元数据（quanto_multiplier / tick / lot / 杠杆上限）几乎不变，所以拉取失败时
            # **回退到上一份缓存**，而不是让整笔操作失败。实测 /contracts 返回约 1.1 MB，
            # 在抖动的 TLS 链路上极易截断（IncompleteRead / handshake timeout）——
            # 模拟盘「public get failed」有 93%（95/102）来自这个端点。
            # 首次调用（无缓存）时仍然抛出，不掩盖真正的不可用。
            if self._contract_cache:
                return self._contract_cache
            stale_cache = self._read_contract_disk(None)
            if stale_cache:
                log.warning("合约元数据拉取失败，回退**过期**磁盘缓存（%d 个合约）",
                            len(stale_cache))
                return stale_cache
            raise
        cache: dict[str, ContractMeta] = {}
        for item in raw:
            name = str(item.get("name") or "")
            if not name:
                continue
            # `quanto_multiplier` **不兜底 1**：缺失就传 None，让
            # `sizing.usd_to_contracts` 按「拒绝下单」处理（T20）。在这里顶一个 1.0 会把
            # 「张数 ↔ 名义金额」差几个数量级，而且让下游的守卫**永不可达** ——
            # 静默错比报错贵得多。其余两项的默认值（1 张步长 / 100 倍杠杆）是无害的。
            qm = item.get("quanto_multiplier")
            cache[name] = ContractMeta(
                name=name,
                quanto_multiplier=float(qm) if qm is not None else None,
                order_size_round=float(item.get("order_size_round") or 1),
                order_price_round=float(item.get("order_price_round") or 0.1),
                leverage_max=int(item.get("leverage_max") or 100),
                # 用 `_f` 而不是 `x or 默认`：`order_size_min=0` 是**真值**，
                # `or` 会把它换成 1，张数边界就一直读错。
                size_min=_f(item.get("order_size_min"), 1.0),
                size_max=_f(item.get("order_size_max"), 0.0),
                enable_decimal=bool(item.get("enable_decimal")),
                price_deviate=_f(item.get("order_price_deviate"), 0.0),
                maintenance_rate=_f(item.get("maintenance_rate"), 0.0),
                maker_fee_rate=_f(item.get("maker_fee_rate"), 0.0),
                taker_fee_rate=_f(item.get("taker_fee_rate"), 0.0),
            )
        self._contract_cache = cache
        self._contract_cache_ts = now
        self._write_contract_disk(cache)
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

    def set_position_mode(self, position_mode: str) -> Any:
        """切换**账户级**持仓模式：`single`（单向）/ `dual`（双向）/ `dual_plus`（分仓）。

        ⚠️ **参数必须走 query string**（Gate 的标量参数约定），不能放 JSON body ——
        实测 2026-10-16：
          `POST /dual_mode`         body `{"dual_mode": false}`       → `MISSING_REQUIRED_PARAM`
          `POST /set_position_mode` body `{"position_mode":"single"}` → `INVALID_PARAM_VALUE`
          `POST /dual_mode?dual_mode=false`                           → ✅ 成功（切到 single）
        错误从「参数缺失」推进到 `ORDER_PENDING` 是参数形态正确的信号。

        ⚠️ 前置条件：该保证金币种下**所有**永续合约无持仓、无挂单（比
        `set_margin_mode` 的单合约限制更严格）—— 有挂单时报 400 `ORDER_PENDING`。

        ⚠️ 切换后**必须清 `_position_mode_cache`** —— `get_position_mode()` 有 30s
        缓存，不清会让引擎在切换后的半分钟里仍按旧模式走分支（下单 body 的
        `auto_size` / `_close` 的 side 校验都读它）。

        ⚠️ 切 `single` 后**必须同时开 `no_flip`**：单向模式下反向单不是开反向仓，
        而是**自动平掉现有仓** —— `no_flip: false` 会让一句 `open_short` 直接平掉多头。
        """
        mode = str(position_mode or "").strip().lower()
        if mode not in ("single", "dual", "dual_plus"):
            raise GateApiError("position_mode must be single|dual|dual_plus")
        if mode == "dual_plus":
            # 分仓模式走 2026-04-30 新增端点（经典 dual_mode 不支持三态）
            path, qs = f"{FUTURES_API}/set_position_mode", "position_mode=dual_plus"
        else:
            path = f"{FUTURES_API}/dual_mode"
            qs = "dual_mode=" + ("true" if mode == "dual" else "false")
        r = self.rest_signed_request("POST", path, qs, None)
        self._position_mode_cache = (None, 0.0)
        return r

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
                side_buy = _f(body.get("size")) > 0
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
        # 触发单张数必须是整数（见 `_int_price_order_size`）—— 所有 price_orders
        # 调用点（stop_entry 保护 / 阶梯 SL / _place_trigger）都从这里过一道。
        body = _int_price_order_size(body)
        result = self.rest_signed_request("POST", f"{FUTURES_API}/price_orders", "", body)
        if not isinstance(result, dict):
            raise GateApiError(f"unexpected place_price_order response: {result!r}")
        return result

    def get_order(self, order_id: str) -> dict:
        """GET one order (any status) — used to confirm it actually landed."""
        return self.rest_signed_request("GET", f"{FUTURES_API}/orders/{order_id}", "") or {}

    def get_price_order(self, price_order_id: str) -> dict:
        return self.rest_signed_request("GET", f"{FUTURES_API}/price_orders/{price_order_id}", "") or {}

    # 挂单/条件单翻页上限：100 条/页 × 10 页。上限的作用是「病态账户不把一轮
    # 扫描拖死」，而不是「我们相信永远不会超过 1000 条」—— 超了会打日志。
    _OPEN_PAGE_SIZE = 100
    _OPEN_MAX_PAGES = 10

    def list_orders(self, contract: Optional[str] = None) -> list:
        """当前挂单（**翻页取全**）。

        Gate 默认一页 100 条，超出就**静默截断** —— 而调用方（executor 的归属扫描、
        watcher 的守护扫描）把这批单当作「本 bot 在该合约上的全部挂单」用。
        截断的后果是**漏判归属**：真实存在的保护单不在结果里 → 对账/清理看不见它
        （fail-open），多币下更容易触发（每合约的单少了，但账户总量会超）。
        """
        return self._list_open_paged(f"{FUTURES_API}/orders", contract)

    def list_price_orders(self, contract: Optional[str] = None) -> list:
        return self._list_open_paged(f"{FUTURES_API}/price_orders", contract)

    def _list_open_paged(self, path: str, contract: Optional[str] = None) -> list:
        qs = "status=open"
        if contract:
            qs += f"&contract={contract}"
        rows: list = []
        seen: set = set()
        for page in range(self._OPEN_MAX_PAGES):
            got = self.rest_signed_request(
                "GET", path,
                f"{qs}&limit={self._OPEN_PAGE_SIZE}&offset={page * self._OPEN_PAGE_SIZE}") or []
            if not got:
                break
            fresh = [r for r in got if self._order_key(r) not in seen]
            if not fresh:
                # 服务端忽略了 offset（或返回了同一页）→ 立刻停：否则会重复 N 倍
                log.warning("%s 翻页无新数据（offset 可能不被支持）→ 提前结束", path)
                break
            for r in fresh:
                seen.add(self._order_key(r))
            rows.extend(fresh)
            if len(got) < self._OPEN_PAGE_SIZE:
                break
        else:
            log.warning("%s 已取满 %d 页（%d 条），可能仍有未取到的单",
                        path, self._OPEN_MAX_PAGES, len(rows))
        return rows

    @staticmethod
    def _order_key(row: Any) -> Any:
        """翻页去重键：优先交易所 id，取不到就退回整条内容的指纹。"""
        if isinstance(row, dict):
            for k in ("id", "order_id", "text"):
                if row.get(k) is not None:
                    return (k, str(row[k]))
            return ("raw", repr(sorted(row.items(), key=lambda kv: str(kv[0]))))
        return ("raw", repr(row))

    def list_position_close(self, contract: Optional[str] = None,
                            limit: int = 100) -> list:
        """平仓历史 —— 每笔带 `pnl`（含手续费与资金费的**已实现盈亏**）。

        **为什么不用 `my_trades`**：实测它返回的字段里**根本没有 pnl**
        （`['amend_text','biz_info','close_size','contract','create_time','fee','id',
        'order_id','point_fee','price','role','size','text']`）—— 只有 size/price/fee，
        推不出已实现盈亏。`position_close` 才是权威口径。

        实测返回样例：`{"contract":"BTC_USDT","pnl":"-1.279122305",
        "pnl_pnl":"-1.03279","pnl_fee":"-0.246332305","accum_size":"41",
        "time":1791302309,"time_us":1791302309972110,"side":"long"}` —— 注意它**没有
        单调 id**，游标只能用 `time_us`（微秒时间戳，唯一）。
        """
        qs = f"limit={max(1, min(int(limit), 1000))}"
        if contract:
            qs += f"&contract={contract}"
        return self.rest_signed_request("GET", f"{FUTURES_API}/position_close", qs) or []

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
            if p.get("contract") == contract and _f(p.get("size")) != 0
        ]
        if side:
            target = [p for p in target if _pos_is_side(p, side)]
        if not target:
            raise GateApiError(f"no position to close on {contract} side={side}")
        if not side:
            side = _infer_side(target[0])
        total = sum(abs(_f(p.get("size"))) for p in target)
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
    size = _f(pos.get("size"))
    mode = str(pos.get("mode") or "").lower()
    if side == "long":
        return size > 0 or mode.endswith("long")
    return size < 0 or mode.endswith("short")


def _infer_side(pos: dict) -> str:
    return "long" if _pos_is_side(pos, "long") else "short"


# 计价币后缀（紧凑写法用）：本仓所有所在 /futures/usdt 路径下只有 USDT 计价，
# USDC 一并识别是为了「加了新计价币时不会静默拼成 X_USDC_USDT」。BTC/ETH 计价
# 走另外的 API 路径，不在本函数的语义范围里。
_QUOTE_SUFFIXES = ("USDT", "USDC")


def resolve_symbol(name: str) -> str:
    """标的写法归一 → 该所合约名（**全仓唯一判据**：AI 工具 / 信号 / 配置 / 采集共用）。

    为什么必须一处实现：同一条判据此前有两份（本文件与 `pa-data-source/kline_watcher.py`），
    语义不同且都处理不了紧凑写法 —— `BTCUSDT` 被拼成 `BTCUSDT_USDT`（合约不存在），
    查询命中 0 行后**静默返回空**，看起来像「这个币没数据」。同一条规则两处实现必然漂移。
    """
    upper = (name or "").upper().strip()
    if not upper:
        raise GateApiError("symbol is required")
    if upper in SYMBOL_MAP:
        return SYMBOL_MAP[upper]
    if "_" in upper:
        # 带分隔符：只认 base 别名（`AU_USDT` → `XAU_USDT`），其余原样返回
        # —— 这样 `PEPE_USDT` 这类不在别名表里的合约不会被二次拼接。
        base = upper.split("_")[0]
        return SYMBOL_MAP.get(base, upper)
    for quote in _QUOTE_SUFFIXES:
        # 紧凑写法：`BTCUSDT` → `BTC_USDT`；base 也要过别名表（`AUUSDT` → `XAU_USDT`）
        if upper.endswith(quote) and len(upper) > len(quote):
            base = upper[: -len(quote)]
            return SYMBOL_MAP.get(base, f"{base}_{quote}")
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


def position_qty(pos: dict, meta: Any = None) -> float:
    """持仓**真实**张数（带符号，小数安全）。

    `/positions` 的 `size` 是 **int64 字段**：`enable_decimal` 合约的小数部分会被
    丢掉（实测 ETH_USDT：真实 16.1 张 → `size` 返回 -16；真实 0.1 张 → 返回 0，
    `mode` 仍是 `dual_long`）。真实张数只能由 `|value| / (mark_price × quanto)` 反算。

    为什么必须这么读：`close_position` 过去拿 `size` 求和当平仓量 → 每轮都少平
    0.1~0.9 张；reduce_only 只减不增 ⇒ 残量永久化（实盘挂过 29 小时幽灵腿）。

    `value` / `mark_price` / `quanto_multiplier` 任一缺失 → 退回 `size` 字段
    （宁可是旧行为，也不要在热路径上抛异常）。方向优先沿用交易所给的 `size` 符号，
    `size==0` 时退回 `mode`（`dual_long` / `dual_short`）。
    """
    raw = _f(pos.get("size"))
    sign = 1.0 if raw > 0 else (-1.0 if raw < 0 else 0.0)
    if sign == 0.0:
        mode = str(pos.get("mode") or "").lower()
        if mode.endswith("long"):
            sign = 1.0
        elif mode.endswith("short"):
            sign = -1.0
    value = _f(pos.get("value"))
    mark = _f(pos.get("mark_price"))
    quanto = _f(getattr(meta, "quanto_multiplier", None)) if meta is not None else 0.0
    if value and mark > 0 and quanto > 0:
        exact = abs(value) / (mark * quanto)
        return math.copysign(exact, sign) if sign else exact
    return raw
