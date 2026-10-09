"""AI-managed runtime event triggers (wake Plan, never place orders).

Storage: history/<bot_id>/ai_triggers.json
Semantics: same tier as interval_sec — hit → run_once (AI re-analyzes).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# 归一必须与**执行器**同源：下单走 `gate_client.resolve_symbol`，触发器白名单若按
# 字面比较，`BTC` 就会被判越界 —— 明明就是同一个币（B-10/B-11 之外的第 3 处静默）。
# 归一实现在此一处，`triggers.py` 复用（同一条判据两处各写一遍必然漂移）。
from ..gate_client import resolve_symbol

DEFAULT_ALLOW = (
    "price_break",
    # —— EMA 家族 ——
    # 状态：价格在 EMA 上/下
    "price_vs_ema",
    # 事件：**价格穿越** EMA（原先缺这个类型，模型只能退而用 price_vs_ema 状态型，
    # 而状态型配冷却等于定时器 —— 线上实测一个 bot 因此每 5 分钟被唤醒，占 43% 轮次）
    "price_cross_ema",
    # 事件：快 EMA 穿慢 EMA
    "ema_cross",
    # 状态：价格 + 双 EMA 排列（多头/空头）
    "ema_stack",
    # 状态：价格偏离 EMA 超过 pct%（乖离）
    "price_ema_dist",
    # 状态：EMA 在最近 bars 根内上行/下行
    "ema_slope",
    "macd_cross",
    "atr_spike",
    "rsi",
    "volume_spike",
    "ma_cross",
    "boll_break",
)

DEFAULT_LIMITS = {
    # `lookback` 下限从 5 抬到 20：5~19 根太小，破位发生得太频繁。
    # 线上实测一个 bot 设 `lookback: 6`（15m 上只有 90 分钟），触发 22 次 /
    # 196 分钟 —— 相当于又一个高频定时器。20 根在 15m 上是 5 小时，破位才有信息量。
    "lookback": (20, 300),
    "period": (2, 200),
    "fast": (2, 200),
    "slow": (2, 200),
    "signal": (2, 50),
    "level": (1.0, 99.0),
    "mult": (1.0, 5.0),
    "pct": (0.05, 20.0),     # price_ema_dist 的偏离百分比
    "bars": (1, 50),         # ema_slope 的回看根数
}

# 每类型的**必填参数**（AI 自设触发器路径）。
# 这些参数的两侧语义**相反**，缺了不能静默取一侧 —— 那是替模型做方向决策，
# 而且它拿到的是「我写对了」的反馈。线上实测：契约漏写 `price_vs_ema` 的 `side`
# → 模型必然不写 → 静默取 `above` → 价格持续在 EMA 上方时条件恒真 →
# 每 300 秒冷却一到就再触发一次（196 分钟 35 次、占全部轮次 43%）。
REQUIRED_PARAMS = {
    "price_break": ("side",),      # high | low
    "price_vs_ema": ("side",),     # above | below
    "boll_break": ("side",),       # upper | lower
}


class TriggerPolicyError(ValueError):
    pass


def normalize_symbol(raw: Any) -> str:
    """归一成执行器认的写法（`btc` / `BTC` → `BTC_USDT`）；空值返回 ""。"""
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    try:
        return resolve_symbol(s)
    except Exception:  # noqa: BLE001 —— 归一失败不该把配置校验炸掉，退回原写法
        return s


def normalize_symbols(seq: Optional[list]) -> tuple:
    """归一 + 去重（保序）。去重是必须的：同一币的两种写法会让「唯一宇宙」判据失真。"""
    out: list[str] = []
    for x in (seq or ()):
        s = normalize_symbol(x)
        if s and s not in out:
            out.append(s)
    return tuple(out)


@dataclass
class AITriggerPolicy:
    enabled: bool = False
    allow_types: tuple = DEFAULT_ALLOW
    max_active: int = 5
    # 每个币的槽位数（缺省 1）；`max_active` 保留为**总上限**。
    # 为什么需要它：`max_active` 全局共享时，首币能把槽位占满 → 其余币**永远设不上
    # 唤醒条件**（弱币系统性出局，且 AI 只看到 `trigger_limit`，无法归因）。
    max_active_per_symbol: int = 1
    default_cooldown_sec: float = 60.0
    default_ttl_sec: float = 86400.0
    allow_modify: bool = True
    allow_symbols: tuple = ()  # empty = use bot symbols
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))


@dataclass
class AITrigger:
    id: str
    type: str
    symbol: str
    params: dict
    cooldown_sec: float = 60.0
    ttl_sec: float = 86400.0
    created_at: float = 0.0
    expire_at: float = 0.0
    last_fire: float = 0.0
    reason: str = ""
    source: str = "llm"

    def to_condition(self) -> dict:
        d = dict(self.params)
        d.update(
            {
                "type": self.type,
                "symbol": self.symbol,
                "cooldown_sec": self.cooldown_sec,
                "key": self.id,
            }
        )
        return d


def validate_trigger_payload(
    raw: dict,
    policy: AITriggerPolicy,
    bot_symbols: Optional[list] = None,
) -> dict:
    """Return normalized params; raise TriggerPolicyError if not allowed."""
    if not isinstance(raw, dict):
        raise TriggerPolicyError("trigger must be object")
    ctype = str(raw.get("type") or "").strip().lower()
    if ctype not in tuple(policy.allow_types):
        raise TriggerPolicyError(f"type not allowed: {ctype!r}")
    symbol = normalize_symbol(raw.get("symbol"))
    # 白名单与输入**两侧都归一**：`BTC` 与 `BTC_USDT` 是同一个币，不该被判越界。
    allowed = normalize_symbols(policy.allow_symbols) or normalize_symbols(bot_symbols)
    if not symbol and len(allowed) == 1:
        # 单币种 bot 漏写 symbol 时意图**没有歧义** —— 直接补上，而不是把整条触发器拒掉。
        # 线上实测：`trigger_rejected: symbol not allowed: ''` 在提示词补全参数范围之后
        # 仍是唯一还在发生的触发器拒绝（2026-10-02 修复后 5 次），纯属白烧一轮。
        # 多币种时仍然拒 —— 那种情况下「用哪个币」是真的猜不出来。
        symbol = allowed[0]
    if allowed and symbol not in allowed:
        # 带上 universe：错误必须能自我纠正，否则模型只能猜（多币下会反复被拒）。
        raise TriggerPolicyError(
            f"symbol not allowed: {symbol!r}; universe={list(allowed)}"
        )

    params: dict[str, Any] = {}
    for key in ("period", "fast", "slow", "signal", "lookback", "mult", "level",
                "side", "dir", "op", "k", "ma", "candles", "pct", "bars"):
        if key in raw and raw[key] is not None:
            params[key] = raw[key]
    # numeric limits: policy.limits overrides DEFAULT_LIMITS
    limits = dict(DEFAULT_LIMITS)
    limits.update(dict(policy.limits or {}))
    for key, (lo, hi) in limits.items():
        if key in params and isinstance(params[key], (int, float)):
            v = float(params[key])
            if v < lo or v > hi:
                raise TriggerPolicyError(f"{key}={v} out of [{lo},{hi}]")
    # 方向类参数必填：缺了不静默取默认值（取错一侧语义相反）
    missing = [k for k in REQUIRED_PARAMS.get(ctype, ()) if k not in params]
    if missing:
        raise TriggerPolicyError(
            f"{ctype} 缺少必填参数 {missing} —— 方向类参数的两侧语义相反，"
            f"不能靠默认值蒙过去"
        )
    cooldown = float(raw.get("cooldown_sec") or policy.default_cooldown_sec)
    ttl = float(raw.get("ttl_sec") or policy.default_ttl_sec)
    if cooldown < 1 or cooldown > 86400:
        raise TriggerPolicyError("cooldown_sec out of range")
    return {
        "type": ctype,
        "symbol": symbol,
        "params": params,
        "cooldown_sec": cooldown,
        "ttl_sec": ttl,
        "reason": str(raw.get("reason") or "")[:200],
    }


class AITriggerStore:
    def __init__(self, path: Path, policy: Optional[AITriggerPolicy] = None):
        self.path = Path(path)
        self.policy = policy or AITriggerPolicy()
        self._items: list[AITrigger] = []
        self.load()

    def load(self) -> None:
        self._items = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        for row in data.get("items") or []:
            try:
                self._items.append(
                    AITrigger(
                        id=str(row.get("id")),
                        type=str(row.get("type")),
                        symbol=str(row.get("symbol")),
                        params=dict(row.get("params") or {}),
                        cooldown_sec=float(row.get("cooldown_sec") or 60),
                        ttl_sec=float(row.get("ttl_sec") or 86400),
                        created_at=float(row.get("created_at") or 0),
                        expire_at=float(row.get("expire_at") or 0),
                        last_fire=float(row.get("last_fire") or 0),
                        reason=str(row.get("reason") or ""),
                        source=str(row.get("source") or "llm"),
                    )
                )
            except Exception:  # noqa: BLE001
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "items": [asdict(t) for t in self._items],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _purge_expired(self, now: Optional[float] = None) -> None:
        ts = now if now is not None else time.time()
        self._items = [t for t in self._items if t.expire_at > ts]

    def active(self, now: Optional[float] = None) -> list[AITrigger]:
        self._purge_expired(now)
        return list(self._items)

    def _per_symbol_cap(self, bot_symbols: Optional[list]) -> Optional[int]:
        """每个币的槽位上限；单币宇宙返回 None（= 不额外收紧）。

        为什么单币不收紧：单币 bot 的 per-symbol 配额与总上限**同义**，再压一层等于
        把 `max_active: 5` 变成 1 —— 那是能力下降（设计 I11 要求单币行为逐字不变），
        而「首币饿死其余币」只存在于多币宇宙。
        """
        universe = normalize_symbols(self.policy.allow_symbols) or normalize_symbols(bot_symbols)
        if len(universe) <= 1:
            return None
        cap = int(getattr(self.policy, "max_active_per_symbol", 1) or 0)
        return cap if cap > 0 else None

    def _counts_by_symbol(self, now: Optional[float] = None) -> dict:
        """按币计数。**与总上限同源**：先做过期清理 —— 过期条件不该继续占槽位。"""
        self._purge_expired(now)
        counts: dict[str, int] = {}
        for t in self._items:
            key = normalize_symbol(t.symbol)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _find_same(self, norm: dict) -> Optional["AITrigger"]:
        """按 (type, symbol, params) 找已存在的同质条件（symbol 归一后比较）。"""
        want_type = str(norm.get("type") or "")
        want_sym = normalize_symbol(norm.get("symbol"))
        want = dict(norm.get("params") or {})
        for t in self._items:
            if t.type == want_type and normalize_symbol(t.symbol) == want_sym and dict(t.params or {}) == want:
                return t
        return None

    def add(self, norm: dict, bot_symbols: Optional[list] = None) -> AITrigger:
        if not self.policy.enabled:
            raise TriggerPolicyError("ai_triggers.enabled=false")
        self._purge_expired()
        # 幂等去重：同 (type, symbol, params) 已存在 → 直接返回它，不新建。
        # 为什么必须去重：同质条件**不占新槽位、也不叠加唤醒频率**。
        # 实测实盘 5 个条件里 4 个是同质 price_break(BTC_USDT)，每个各按自己的
        # cooldown 唤醒一次，聚合起来把节奏从 15 分钟压到约 1 分钟 —— 每轮一次
        # LLM 调用。AI 侧已给它可见性（prompt 里列出生效触发器），这里是兜底。
        # 注意：不刷新 TTL —— 否则条件永不失效，TTL 就失去清理意义。
        dup = self._find_same(norm)
        if dup is not None:
            return dup
        symbol = normalize_symbol(norm.get("symbol"))
        per_symbol = self._per_symbol_cap(bot_symbols)
        if per_symbol is not None:
            used = self._counts_by_symbol().get(symbol, 0)
            if used >= per_symbol:
                raise TriggerPolicyError(
                    f"trigger_limit: {symbol} 已有 {used} 个条件，"
                    f"max_active_per_symbol={per_symbol} —— "
                    f"换条件请先 trigger_ops remove 该币的旧条件"
                )
        if len(self._items) >= int(self.policy.max_active):
            raise TriggerPolicyError(f"trigger_limit: max_active={self.policy.max_active}")
        now = time.time()
        t = AITrigger(
            id="t-" + uuid.uuid4().hex[:8],
            type=norm["type"],
            symbol=symbol,
            params=norm.get("params") or {},
            cooldown_sec=float(norm.get("cooldown_sec") or self.policy.default_cooldown_sec),
            ttl_sec=float(norm.get("ttl_sec") or self.policy.default_ttl_sec),
            created_at=now,
            expire_at=now + float(norm.get("ttl_sec") or self.policy.default_ttl_sec),
            reason=norm.get("reason") or "",
        )
        self._items.append(t)
        self.save()
        return t

    def remove(self, trigger_id: str) -> bool:
        if not self.policy.allow_modify:
            raise TriggerPolicyError("allow_modify=false")
        before = len(self._items)
        self._items = [t for t in self._items if t.id != trigger_id]
        changed = len(self._items) != before
        if changed:
            self.save()
        return changed

    def replace_all(self, norms: list[dict], bot_symbols: Optional[list] = None) -> list[AITrigger]:
        if not self.policy.allow_modify:
            raise TriggerPolicyError("allow_modify=false")
        self._items = []
        out = []
        for n in norms:
            out.append(self.add(n, bot_symbols=bot_symbols))
        return out

    def mark_fired(self, trigger_id: str, ts: Optional[float] = None) -> None:
        for t in self._items:
            if t.id == trigger_id:
                t.last_fire = ts if ts is not None else time.time()
        self.save()
