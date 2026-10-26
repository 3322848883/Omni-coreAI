"""Plan / chips schema for LLM strategist (multi-symbol decisions)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..schema import SchemaError, tp_fields

log = logging.getLogger(__name__)

CHIP_ACTIONS = {
    "open_long",
    "open_short",
    "add_long",
    "add_short",
    "reduce_long",
    "reduce_short",
    "close",
    "close_all",
    "hold",
    "stop_entry_long",
    "stop_entry_short",
    "flatten",
    "cancel_all",
    "cancel_price_all",
    "modify_tp_sl",
}

CHIP_ORDER_TYPES = {"market", "limit", "post_only", "ioc", "fok"}

# 契约 Tier 1：区域四选一（见 docs/compose/spec/pa-skills-upgrade.md [S2]）
# `trend_range`（2026-10-22 新增）= 趋势中的旗形 / 深度回调：高周期趋势成立 +
# 本周期出现 ≥2 腿回调且边界反复。没有这个值时模型只能在 `trend`（要给尾仓）与
# `range`（必须放弃双止盈）之间二选一，实测因此把判定倒推成「我想只做 scalp，
# 所以它是 range」—— 判定被字段需求反向驱动。它**不受** range 的多档 TP 限制。
# 顺序即语义优先级：从「最趋势」到「最逆势」，契约按此顺序渲染枚举。
# 系统提示词**不再手写** region 枚举（原先写「三选一 trend|range|reversal」，
# 与这里的四值长期不一致，模型因此不敢用 `trend_range`）。
REGION_ORDER = ["trend", "trend_range", "range", "reversal"]
REGIONS = set(REGION_ORDER)

# 模型常把「触发后按市价/限价成交」写成 stop_market / stop_limit，或把
# tp_mode/sl_mode 的值（trigger）误写进 type。schema 用 `trigger_price` + `type`
# 表达同一件事，所以这里把别名归一 —— 否则整笔信号会被 executor 拒掉。
CHIP_TYPE_ALIASES = {
    "trigger": "market", "stop": "market", "stop_order": "market",
    "tp": "market", "sl": "market", "tp_sl": "market",
    "stop_loss": "market", "take_profit": "market",
    "stop_market": "market", "stop_limit": "limit",
    "trigger_market": "market", "trigger_limit": "limit",
}


def normalize_chip_type(raw: Any) -> Optional[str]:
    """把模型写的订单类型归一到 `CHIP_ORDER_TYPES`；不支持返回 None。

    **讨论环节也要用**：`_discussion_chip` 绕过了 `parse_plan`，模型在那里写
    `stop_market` 会一路进到信号里，被 executor 以 `unsupported type` 整笔拒掉。
    """
    t = str(raw or "market").strip().lower()
    t = CHIP_TYPE_ALIASES.get(t, t)
    return t if t in CHIP_ORDER_TYPES else None


class PlanError(Exception):
    pass


def _safe_symbol(symbol: str, universe: Optional[list[str]] = None) -> str:
    """归一 symbol 并（可选）校验它属于**品种宇宙**。

    2026-10-26 回归修复：08:xx 之后这个函数与 `parse_plan`/`parse_plan_text` 的
    `symbols` 参数被一起删掉了，而 `loop.py` 仍在传 `symbols=` →
    每一轮 `TypeError: parse_plan_text() got an unexpected keyword argument 'symbols'`
    （martin-grid 102 次 / pa-eth-v5 194 次，两个实盘 bot 全程 0 计划）。

    宇宙由调用方传入（`parse_plan(..., symbols=)`）：只有程序层知道这个 bot 管哪些币。
    越界必须在 plan 层就拒（证据 B-5：原先只查字符集，越界 chip 静默进 inbox）。

    **异常统一成 `PlanError`**：调用方只捕 `PlanError`（`loop.py` 的 `except PlanError`、
    `__main__.cmd_plan` 连 try 都没有），`resolve_symbol` 抛的 `GateApiError` 漏出去会让
    `plan` 直接 traceback、`plan-loop` 绕过 `degraded` 与 `_record_cycle_failure`。
    """
    import re
    from ..gate_client import resolve_symbol
    try:
        s = resolve_symbol(str(symbol or ''))
    except Exception as e:  # noqa: BLE001 — 见 docstring：一律收口成 PlanError
        raise PlanError(f'symbol invalid: {symbol!r}') from e
    if not re.fullmatch(r'[A-Z0-9_]{2,20}', s or ''):
        raise PlanError(f'symbol invalid: {symbol!r}')
    if universe is not None:
        uni = {str(x or '').strip().upper() for x in universe if str(x or '').strip()}
        if s not in uni:
            raise PlanError(
                f'symbol not in universe: {s!r} (allowed: {",".join(sorted(uni)) or "未配置"})')
    return s


@dataclass
class Chip:
    symbol: str
    action: str
    confidence: float = 0.0
    size_usd: Optional[float] = None
    size: Optional[float] = None      # 小数张：ETH 步长 0.1，模型会写 0.7
    tp: Optional[float] = None
    sl: Optional[float] = None
    # 多级止盈：档位清单与解析统一在 `omnialpha.schema.tp_fields`（唯一来源），
    # 这里只做承载 —— 少了 tp_extra 就会把模型写的第 4 档起**静默丢掉**。
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp1_share: Optional[float] = None
    tp2_share: Optional[float] = None
    tp3_share: Optional[float] = None
    tp_extra: list[float] = field(default_factory=list)
    tp_extra_shares: list[Optional[float]] = field(default_factory=list)
    order_type: str = "market"
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    leverage: Optional[int] = None
    side: Optional[str] = None  # long|short — dual-position manage (close/modify_tp_sl)
    tp_mode: str = "trigger"  # trigger | limit_order
    sl_mode: str = "trigger"
    reasoning: str = ""
    # ── 契约 Tier 1（docs/compose/spec/pa-skills-upgrade.md [S2]）──
    # 全部 optional：缺省不改变任何现有行为。由策略层与记忆层消费；
    # executor 忽略这些字段，下单映射与风控闸门完全不变。
    region: str = ""                       # trend | range | reversal
    invalidation: Optional[float] = None   # 前提失效价：触及即视为结构破坏
    time_stop_bars: Optional[int] = None   # 最大持仓轮数
    give_back_pct: Optional[float] = None  # 浮盈回撤阈值(%)
    risk_pct: Optional[float] = None       # 本单实际风险占权益比例
    # 网格档目标止盈（= 本档与上一档的中值）。挂单时 `tp` 只能写占位价 ——
    # 交易所要求多单 tp > mark，而中值必在现价下方，写了会整笔中止；
    # 所以真值放这里，成交后由下一轮 `modify_tp_sl` 把 tp 改过去。
    tp_target: Optional[float] = None
    # 通用自证检查（模型写在 `meta.checks`）：`[{id, inputs, calc, verdict}]`。
    # 系统层只做「inputs 与快照对账 + 留痕 + 可选闸门（`strategist.require_checks`）」，
    # **不解释 id / calc / verdict 的语义** —— 那是人格的事。
    # **不进 `to_signal_dict`**：每轮几百字符，进订单上下文会污染缓存前缀。
    checks: list[dict] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    scenarios: dict = field(default_factory=dict)
    # ── 逐K形态读（人格可选产出，见 prompts/brooks_btc_pa.md）──
    # 契约**不要求**这两个字段：只有明确要求它们的人格（当前是 brooks-btc）才会填，
    # 其他 bot 留空，行为零变化。
    # 它们**不进 `to_signal_dict`**（= 不进 inbox / 不进订单上下文）—— 每轮 ~1.2KB，
    # 进了下一轮的 prompt 会污染缓存前缀。只作审计落 `thinking.json`。
    kline_tf: str = ""                              # 读的是哪个周期：5m / 15m / 1h…
    kline_read: list[str] = field(default_factory=list)   # 由新到旧的逐根定性

    def to_signal_dict(self) -> dict:
        d: dict[str, Any] = {"action": self.action, "symbol": self.symbol, "type": self.order_type}
        if self.size is not None:
            d["size"] = self.size
        if self.size_usd is not None:
            d["size_usd"] = self.size_usd
        if self.price is not None:
            d["price"] = self.price
        if self.tp is not None:
            d["tp"] = self.tp
        if self.sl is not None:
            d["sl"] = self.sl
        if self.tp2 is not None:
            d["tp2"] = self.tp2
        if self.tp3 is not None:
            d["tp3"] = self.tp3
        if self.tp1_share is not None:
            d["tp1_share"] = self.tp1_share
        if self.tp2_share is not None:
            d["tp2_share"] = self.tp2_share
        if self.tp3_share is not None:
            d["tp3_share"] = self.tp3_share
        # 第 4 档起（tp4/tp5）—— 档位清单唯一来源，写得出就带得走
        for idx, px in enumerate(self.tp_extra):
            base = f"tp{idx + 4}"
            d[base] = px
            sh = self.tp_extra_shares[idx] if idx < len(self.tp_extra_shares) else None
            if sh is not None:
                d[f"{base}_share"] = sh
        if self.trigger_price is not None:
            d["trigger_price"] = self.trigger_price
        if self.leverage is not None:
            d["leverage"] = self.leverage
        if self.side:
            d["side"] = self.side
        if self.tp_mode and self.tp_mode != "trigger":
            d["tp_mode"] = self.tp_mode
        if self.sl_mode and self.sl_mode != "trigger":
            d["sl_mode"] = self.sl_mode
        if self.region:
            d["region"] = self.region
        if self.invalidation is not None:
            d["invalidation"] = self.invalidation
        if self.time_stop_bars is not None:
            d["time_stop_bars"] = self.time_stop_bars
        if self.give_back_pct is not None:
            d["give_back_pct"] = self.give_back_pct
        if self.risk_pct is not None:
            d["risk_pct"] = self.risk_pct
        if self.rule_ids:
            d["rule_ids"] = list(self.rule_ids)
        if self.scenarios:
            d["scenarios"] = dict(self.scenarios)
        if self.reasoning:
            d.setdefault("meta", {})["reasoning"] = self.reasoning
        if self.tp_target is not None:
            d.setdefault("meta", {})["tp_target"] = self.tp_target
        return d


@dataclass
class Plan:
    cycle_id: str
    reasoning: str = ""
    chips: list[Chip] = field(default_factory=list)
    triggers: list = field(default_factory=list)
    trigger_ops: list = field(default_factory=list)
    rejected_triggers: list = field(default_factory=list)
    # 本轮引用了哪些历史决策（模型自报）。prompt 一直在要求它输出，但这里原先没有
    # 这个字段、`parse_plan` 也不取 —— 于是它进了 `raw` 就再没人看，实测 896 条
    # journal 里非空 0 条。是「要求了但不消费」的那类缺陷。
    memory_refs: list = field(default_factory=list)
    # 解析期的**宽容告警**（如 `scenarios` 非对象被丢弃）。为什么不抛 PlanError：
    # 契约里早已写明「不要写 scenarios、无消费方」，一个无人消费的字段不该杀死整轮决策
    # （旧实现实测：18% 的轮次因格式抖动整轮产出归零）。
    notes: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _f(v, name: str = "", idx: Optional[int] = None) -> Optional[float]:
    """把模型给的值转 float；**坏输入抛 `PlanError` 而不是 `ValueError`**。

    调用方**只捕 `PlanError`**（`loop.py` 的 `except PlanError`；`__main__.cmd_plan`
    连 try 都没有）。直转 `float()` 会把 `ValueError` 漏出去 —— 单次 `plan` 直接
    traceback，`plan-loop` 虽被外层兜住却**绕过 `degraded` 与 `_record_cycle_failure`**，
    于是 `plan_fail` 告警与 `error_streak` 静默不计数。实测来自独立评审（Critical 2）。
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        where = f"chips[{idx}].{name}" if idx is not None else (name or "value")
        raise PlanError(f"{where} must be a number, got {v!r}")


def _int(v, name: str, idx: int) -> Optional[int]:
    """同 `_f`，整数版。`time_stop_bars="8bars"` 这类输入必须走 PlanError。"""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise PlanError(f"chips[{idx}].{name} must be an integer, got {v!r}")


def _qty(v, name: str, idx: int) -> Optional[float]:
    """张数解析（**小数张**）：`0.7` / `"0.7"` 都保留。

    原来用 `_int`：`int(0.7)` → **0**（模型明确要 0.7 张，静默变「没给量」甚至
    0 张单）；`int("0.7")` → `PlanError`（整轮计划作废）。ETH/15 个小数张合约上
    这两种都是错的。合法值必须原样传到执行层由 `_resolve_order_size` 统一定夺
    （包括「小于一个步长就拒单」）。
    """
    if v is None or v == "":
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        raise PlanError(f"chips[{idx}].{name} must be a number, got {v!r}")
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        raise PlanError(f"chips[{idx}].{name} must be finite, got {v!r}")
    return out


def _memory_refs(raw: Any) -> list[str]:
    """规范化 `memory_refs`：只留非空**字符串**，最多 20 条。

    **不抛 `PlanError`** —— 与 chips 的字段不同，这是**观测性**字段（记录本轮引用了
    哪些历史决策），格式瑕疵不该让整轮决策作废。非法输入一律降级为 `[]`。

    非字符串元素（数字/字典）**直接丢弃**而不是 `str()` 强转：引用的是 `cycle_id`，
    把 `{"a": 1}` 变成 `"{'a': 1}"` 只是往 journal 里塞垃圾。
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if s:
            out.append(s)
        if len(out) >= 20:
            break
    return out


def _kline_read(raw: Any) -> list[str]:
    """规范化逐K读：只留非空**字符串**，最多 30 条。

    与 `_memory_refs` 同属**观测性**字段 —— 不参与下单、不影响风控，格式坏掉
    不该让整轮降级成 hold，所以**不抛 `PlanError`**。非字符串元素直接丢弃。
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if s:
            out.append(s)
        if len(out) >= 30:
            break
    return out


def extract_kline_reads(text: str, default_tf: str = "") -> dict:
    """从模型正文里轻量抽出逐K读，供 `thinking.json` 审计落盘。

    为什么不复用 `parse_plan`：两个 `_save_thinking` 调用点都在 `parse_plan_text`
    **之前**（见 `loop.run_once` / `analyze_once`），那时还没有 Plan 对象。而为了
    这个审计字段去调整落盘顺序，会把「解析失败也要留下 CoT」这条现有行为改掉。

    不抛异常：正文坏掉、没有该字段、chips 为空都返回 `{}`（调用方据此跳过落盘）。
    返回 `{"kline_tf": str, "kline_read": [str]}`，两者皆空时返回 `{}`。

    `default_tf`（调用方传本轮主周期）只解决**模型漏写**：`kline_tf` 是人格契约里
    的必填项，却没有任何引擎消费方，只进审计 —— 漏写不会报错，只会让事后核对
    「它读的是哪个周期」没有答案（2026-10-25 实跑 `brooks-eth` 即此形态：20 条
    逐K全对、`kline_tf` 空）。它**只在有 `kline_read` 时才补**：空手补一个周期
    等于伪造审计记录。
    """
    try:
        data = _extract_json_object(text or "")
    except Exception:  # noqa: BLE001 — 纯观测，任何异常都只是「这轮没抽到」
        return {}
    if not isinstance(data, dict):
        return {}
    tf, bars = "", []
    for c in (data.get("chips") or []):
        if not isinstance(c, dict):
            continue
        if not tf:
            tf = str(c.get("kline_tf") or "").strip()[:16]
        got = _kline_read(c.get("kline_read"))
        if got:
            bars = got
            break
    if bars and not tf:
        tf = str(default_tf or "").strip()[:16]
    if not bars and not tf:
        return {}
    return {"kline_tf": tf, "kline_read": bars}


# ── 通用自证检查 `meta.checks`（系统层）──────────────────────────────
# 分层边界（2026-10-25）：**系统层只提供机制，不认识任何流派名词。**
# 人格负责「要检查什么、阈值多少、结论怎么叫」；引擎只负责：
#   ① 把它声明的 `inputs`（读了快照哪些值）与快照对账
#   ② 把 `calc` / `verdict` 原样留痕
# 所以这里不出现任何流派专有名词（检查项 id、阈值、结论口径全归人格）。
#
#   {"id": "HC-03",                              # 人格自定义，引擎不解释
#    "inputs":  {"atr14": 6.09},                 # 声明读了哪些快照值（**会对账**）
#    "calc":    {"dist": 16.4, "lower": 9.14},   # 中间计算（只留痕）
#    "verdict": "偏宽"}                          # 结论（只留痕）
CHECK_TOL = 0.05


def _coerce_num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_check(raw: Any, action: str = "", symbol: str = "") -> Optional[dict]:
    """把模型写的一条 check 规整成 `{id, action, symbol, inputs, calc, verdict, text}`。"""
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        return {"id": "", "action": action, "symbol": symbol, "inputs": {},
                "calc": {}, "verdict": "", "text": s[:400]}
    if not isinstance(raw, dict):
        return None
    ins = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
    calc = raw.get("calc") if isinstance(raw.get("calc"), dict) else {}
    return {
        "id": str(raw.get("id") or "").strip()[:40],
        "action": action, "symbol": symbol,
        "inputs": {str(k)[:40]: v for k, v in ins.items()},
        "calc": {str(k)[:40]: v for k, v in calc.items()},
        "verdict": str(raw.get("verdict") or "").strip()[:40],
        "text": str(raw.get("note") or "").strip()[:400],
    }


def _checks_from_chip(c: dict, action: str = "", symbol: str = "") -> list[dict]:
    """从一个 chip 里取 `meta.checks`，并兼容旧式 `meta.<name>_check` 自由文本。

    兼容是为了不打断既有实盘人格（旧人格把检查写成 `meta.<name>_check` 自由文本）：按文本
    留痕（`id` = 键名去掉后缀），不对账、不闸门，直到人格改成 `checks` 为止。
    """
    meta = c.get("meta") if isinstance(c.get("meta"), dict) else {}
    items = meta.get("checks", c.get("checks"))
    out: list[dict] = []
    if isinstance(items, list):
        for it in items:
            n = _norm_check(it, action, symbol)
            if n:
                out.append(n)
    elif items is not None:
        n = _norm_check(items, action, symbol)
        if n:
            out.append(n)
    for k, v in meta.items():
        if not isinstance(k, str) or k == "checks" or not k.endswith("_check"):
            continue
        s = str(v or "").strip()
        if s:
            out.append({"id": k[: -len("_check")], "action": action, "symbol": symbol,
                        "inputs": {}, "calc": {}, "verdict": "", "text": s[:400]})
    return out


def extract_checks(text: str) -> list[dict]:
    """抽出每个 chip 的自证检查（供审计落盘 / 可选闸门）。

    与 `extract_regions` 同一理由：`_save_thinking` 在 `parse_plan_text` 之前跑，
    不能为了审计字段改落盘顺序。**不抛异常** —— 正文坏掉/没写/为空都返回 `[]`。
    """
    try:
        data = _extract_json_object(text or "")
    except Exception:  # noqa: BLE001 — 纯观测
        return []
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    chips = [c for c in (data.get("chips") or []) if isinstance(c, dict)]
    for c in chips:
        out.extend(_checks_from_chip(
            c,
            str(c.get("action") or "").strip().lower(),
            str(c.get("symbol") or "").strip()))
    # 计划级 `meta`：实测模型会把检查写在**顶层**（2026-10-25 pa-eth-v5 那轮：
    # `{"chips":[...],"triggers":[],"meta":{"stop_width_check":...}}`）。
    # 只有 1 个 chip 时归给它；多个 chip 则不归属（action/symbol 留空），仍留痕。
    plan_meta = data.get("meta")
    if isinstance(plan_meta, dict) and chips:
        only = chips[0] if len(chips) == 1 else {}
        out.extend(_checks_from_chip(
            {"meta": plan_meta},
            str((only or {}).get("action") or "").strip().lower(),
            str((only or {}).get("symbol") or "").strip()))
    return out


def _snapshot_lookup(entry: Any, key: str) -> tuple[bool, Any]:
    """按 `entry[key]` → `entry["indicators"][key]` → `entry["structure"][key]` 取值。

    写全路径（`indicators.atr14` / `structure.er`）也认。命中返回 `(True, value)`。
    这样人格只需声明「我用了 atr14」或「我用了 er」，不用关心它在快照哪一层。
    """
    if not isinstance(entry, dict) or not key:
        return False, None
    cur: Any = entry
    for part in str(key).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            cur = None
            break
    if cur is not None:
        return True, cur
    for block in ("indicators", "structure"):
        b = entry.get(block)
        if isinstance(b, dict) and key in b:
            return True, b.get(key)
    return False, None


def verify_checks(check: dict, entry: Any, tol: float = CHECK_TOL) -> dict:
    """只对账 `inputs`：模型声称读了 `key=value`，快照里的同名值必须对得上。

    **`calc` / `verdict` 一律不判** —— 那是人格的语义，引擎无权解释。
    状态：`ok` / `mismatch`（写了但对不上）/ `unverified`（没写 inputs，多为旧式自由文本）。
    """
    inputs = check.get("inputs") or {}
    issues: list[str] = []
    checked = 0
    for k, v in inputs.items():
        found, sv = _snapshot_lookup(entry, k)
        if not found:
            issues.append("快照里没有 %s（声明 %s）" % (k, v))
            continue
        dn, sn = _coerce_num(v), _coerce_num(sv)
        if dn is None or sn is None:
            if str(v) != str(sv):
                issues.append("%s 不一致（声明 %s / 快照 %s）" % (k, v, sv))
            continue
        base = abs(sn)
        if base and abs(dn - sn) / base > tol:
            issues.append("%s 偏差 %.1f%%（声明 %g / 快照 %g）"
                          % (k, abs(dn - sn) / base * 100, dn, sn))
        checked += 1
    if not inputs:
        return {"status": "unverified", "checked": 0,
                "issues": issues or ["没写 inputs（未声明用了哪些快照值）"]}
    return {"status": "mismatch" if issues else "ok",
            "checked": checked, "issues": issues}


def extract_regions(text: str) -> list[dict]:
    """抽出每个 chip 声明的 `action` / `symbol` / `region`（区域判定留痕）。

    为什么不能用裸 `json.loads`：2026-10-25 实测模型会把 Plan 包进
    ` ```json ` 围栏并在后面追加一段说明文字（当轮 2,824 字符，JSON 只占前段）。
    引擎的 `parse_plan_text` 有容错所以照常成交，**但留痕侧用 json.loads 会直接
    失败** → `regions` 空、region 看起来像「没判」。这里复用同一套容错提取。
    """
    try:
        data = _extract_json_object(text or "")
    except Exception:  # noqa: BLE001 — 纯观测
        return []
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for c in (data.get("chips") or []):
        if not isinstance(c, dict):
            continue
        reg = str(c.get("region") or "").strip().lower()
        if not reg:
            continue
        out.append({
            "action": str(c.get("action") or "").strip().lower(),
            "symbol": str(c.get("symbol") or "").strip(),
            "region": reg,
        })
    return out


def parse_plan(data: Any, symbols: Optional[list[str]] = None) -> Plan:
    """解析 Plan。`symbols` 非 None 时校验每个 chip 的 symbol 属于该宇宙。"""
    if not isinstance(data, dict):
        raise PlanError("plan must be a JSON object")
    notes: list[str] = []
    chips_raw = data.get("chips") or []
    if not isinstance(chips_raw, list):
        raise PlanError("chips must be an array")
    if len(chips_raw) > 50:
        raise PlanError("chips too large (max 50)")
    chips: list[Chip] = []
    for i, raw in enumerate(chips_raw):
        if not isinstance(raw, dict):
            raise PlanError(f"chips[{i}] must be object")
        action = str(raw.get("action") or "").strip().lower()
        order_type_raw = str(raw.get("type") or "market").strip().lower()
        # recovery: model sometimes puts action name in `type` (e.g. type=stop_entry_long)
        if order_type_raw not in CHIP_ORDER_TYPES and order_type_raw in CHIP_ACTIONS:
            if not action or action == "hold":
                action = order_type_raw
            order_type_raw = "market"
        if action not in CHIP_ACTIONS:
            raise PlanError(f"chips[{i}].action unsupported: {action!r}")
        symbol = str(raw.get("symbol") or "").strip()
        if not symbol:
            raise PlanError(f"chips[{i}].symbol required")
        try:
            conf = float(raw.get("confidence") or 0)
        except (TypeError, ValueError):
            raise PlanError(f"chips[{i}].confidence must be a number, got {raw.get('confidence')!r}")
        if not 0 <= conf <= 1:
            if 0 < conf <= 100:
                conf = conf / 100.0
            else:
                raise PlanError(f"chips[{i}].confidence out of range")
        order_type = normalize_chip_type(order_type_raw)
        if order_type is None:
            raise PlanError(f"chips[{i}].type unsupported: {order_type_raw!r}")
        side = raw.get("side")
        if side is not None:
            side = str(side).strip().lower()
            if side not in ("long", "short"):
                raise PlanError(f"chips[{i}].side must be long|short, got {raw.get('side')!r}")
        tp_mode = str(raw.get("tp_mode") or "trigger").lower()
        sl_mode = str(raw.get("sl_mode") or "trigger").lower()
        for name, m in (("tp_mode", tp_mode), ("sl_mode", sl_mode)):
            if m not in ("trigger", "limit_order", "limit"):
                raise PlanError(f"chips[{i}].{name} unsupported: {m!r}")
        # ── 契约 Tier 1（docs/compose/spec/pa-skills-upgrade.md [S2]）──
        region = str(raw.get("region") or "").strip().lower()
        if region and region not in REGIONS:
            raise PlanError(
                f"chips[{i}].region must be one of {'|'.join(sorted(REGIONS))}, "
                f"got {raw.get('region')!r}")
        # 多级止盈：解析走 `omnialpha.schema.tp_fields`（唯一来源，含 tp3/tp4/tp5）。
        # 原先这里自己写一份字段清单 —— 与执行侧那份不一致就是「静默丢档」的来源。
        try:
            tf = tp_fields(raw)
        except SchemaError as e:
            raise PlanError(f"chips[{i}].{e}") from e
        tp2 = tf["tp2"]
        # 区域=区间 → 禁止 2R 目标。把提示词那条规则（「区间只做 scalp，
        # 禁止持有 2R 目标」）从一句话变成程序约束 —— 否则区域判定只是模型
        # 自己贴的标签，为用双止盈改判成趋势就绕过去了（实测发生过）。
        # **第 2 档起全部算**：只挡 tp2 的话写 tp3 就绕过去了。
        if region == "range" and (tf["tp2"] is not None or tf["tp3"] is not None
                                  or tf["tp_extra"]):
            raise PlanError(
                f"chips[{i}].tp2 must be empty when region=range "
                f"(区间只做 scalp，禁止持有 2R 目标)")
        rule_ids_raw = raw.get("rule_ids") or []
        if not isinstance(rule_ids_raw, list):
            raise PlanError(f"chips[{i}].rule_ids must be an array")
        scenarios_raw = raw.get("scenarios") or {}
        if not isinstance(scenarios_raw, dict):
            # 丢弃 + 告警，**不整轮作废**：scenarios 全仓无消费方，
            # 而嵌套对象正是 JSON 解析失败的头号来源。
            notes.append(
                f"chips[{i}].scenarios 非对象已丢弃（got {type(scenarios_raw).__name__}）")
            log.warning("plan chips[%d].scenarios 非对象已丢弃：%r", i, scenarios_raw)
            scenarios_raw = {}
        # 目标止盈：模型写在 `meta.tp_target`（网格档成交后要改成的中值价）。
        # 执行辅助字段 —— 一律**宽松解析**，非法/缺失置 None，绝不因此拒掉整轮。
        # 通用自证检查：宽松解析（缺失/非法 → 空表，绝不因此拒整轮；
        # 是否据此拦单由 `strategist.require_checks` 在 loop 层决定）
        checks_raw = _checks_from_chip(raw)
        tp_target_raw = raw.get("tp_target")
        if tp_target_raw is None and isinstance(raw.get("meta"), dict):
            tp_target_raw = (raw.get("meta") or {}).get("tp_target")
        if tp_target_raw is None:
            # 模型习惯把目标价写成 "tp_target=2426整格" / "tp_target=前低2393.55" 塞进
            # reasoning 文本（2026-10-22 连续 4 轮：字段全空，文字里全有，保护单全退回
            # 占位价 2333.42）。这里只**提取模型自己写出的数字**，程序不造价格；
            # 取不到就保持 None（引擎会跳过并留痕 no_tp_target）。
            _txt = " ".join(
                str(x) for x in (
                    raw.get("reasoning") or "",
                    (raw.get("meta") or {}).get("reasoning") if isinstance(raw.get("meta"), dict) else "",
                    (raw.get("meta") or {}).get("chips_reasoning") if isinstance(raw.get("meta"), dict) else "",
                )
            )
            _m = re.search(
                r"tp_target\s*[=:：]?\s*[^0-9\n]{0,12}?([0-9]+(?:\.[0-9]+)?)", _txt)
            if not _m:
                # 模型还常用更短的写法：`tp2376` / `tp=2376` / `tp 2376`
                _m = re.search(r"\btp\s*[=:：]?\s*([0-9]{3,}(?:\.[0-9]+)?)", _txt)
            if _m:
                tp_target_raw = _m.group(1)
        try:
            tp_target = float(tp_target_raw) if tp_target_raw not in (None, "") else None
        except (TypeError, ValueError):
            tp_target = None
        # 逐K读是观测性字段，一律宽松处理（不抛 PlanError，见 _kline_read 说明）
        kline_tf_raw = str(raw.get("kline_tf") or "").strip()[:16]
        chips.append(
            Chip(
                symbol=_safe_symbol(symbol, symbols),
                action=action,
                confidence=conf,
                size_usd=_f(raw.get("size_usd"), "size_usd", i),
                size=_qty(raw.get("size"), "size", i),
                sl=_f(raw.get("sl"), "sl", i),
                **tf,
                order_type=order_type,
                price=_f(raw.get("price"), "price", i),
                trigger_price=_f(raw.get("trigger_price"), "trigger_price", i),
                leverage=_int(raw.get("leverage"), "leverage", i),
                side=side,
                tp_mode="limit_order" if tp_mode == "limit" else tp_mode,
                sl_mode="limit_order" if sl_mode == "limit" else sl_mode,
                reasoning=str(raw.get("reasoning") or ""),
                region=region,
                invalidation=_f(raw.get("invalidation"), "invalidation", i),
                time_stop_bars=_int(raw.get("time_stop_bars"), "time_stop_bars", i),
                give_back_pct=_f(raw.get("give_back_pct"), "give_back_pct", i),
                risk_pct=_f(raw.get("risk_pct"), "risk_pct", i),
                tp_target=tp_target,
                rule_ids=[str(x) for x in rule_ids_raw],
                scenarios=dict(scenarios_raw),
                checks=checks_raw,
                kline_tf=kline_tf_raw,
                kline_read=_kline_read(raw.get("kline_read")),
            )
        )
    triggers = data.get("triggers") or []
    if not isinstance(triggers, list):
        raise PlanError("triggers must be an array")
    trigger_ops = data.get("trigger_ops") or []
    if not isinstance(trigger_ops, list):
        raise PlanError("trigger_ops must be an array")
    # 计划级 `meta` 里的检查：只有 1 个 chip 且它自己没写 checks 时归给它
    # （模型实测会把 `meta` 写在顶层；不认这一层，v5 的宽度留痕就一直空着）
    _pm = data.get("meta")
    if isinstance(_pm, dict) and len(chips) == 1 and not chips[0].checks:
        _pl = _checks_from_chip({"meta": _pm}, chips[0].action, chips[0].symbol)
        if _pl:
            chips[0].checks = _pl

    return Plan(
        cycle_id=str(data.get("cycle_id") or ""),
        reasoning=str(data.get("reasoning") or ""),
        chips=chips,
        triggers=triggers,
        trigger_ops=trigger_ops,
        memory_refs=_memory_refs(data.get("memory_refs")),
        notes=notes,
        raw=data,
    )


def _repair_json(t: str) -> str:
    """修复 LLM 输出的常见 JSON 病：Markdown 围栏、注释、尾逗号、单引号、裸键名。"""
    import re

    s = t.strip()
    # 1) 剥离 Markdown 围栏（前后都要）
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    # 2) 去掉 // 与 /* */ 注释（字符串外的简化处理）
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"(^|[^:])//.*?$", r"\1", s, flags=re.MULTILINE)
    # 3) 尾逗号 , 后跟 } 或 ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # 4) 单引号字符串 → 双引号（仅当该串无双引号）
    def _sq(m):
        inner = m.group(1)
        return '"' + inner + '"' if '"' not in inner else m.group(0)
    s = re.sub(r"'([^']*)'", _sq, s)
    # 5) 裸键名 { key: → { "key":
    s = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)", r'\1"\2"\3', s)
    # 6) True/False/None → true/false/null
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b", "null", s)
    # 7) 按栈补齐未闭合的 { / [（截断/漏括号的常见形态）
    s = _close_tail(s)
    # 8) 补括号可能把「末尾逗号」暴露成 `,}` —— 再清一次，否则仍然是非法 JSON
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _iter_json_objects(text: str):
    """扫描文本中所有平衡的 {..} 片段（跳过字符串内的花括号）。"""
    t = text or ""
    i = 0
    n = len(t)
    while i < n:
        if t[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = t[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield t[i : j + 1]
                    break
            j += 1
        i = (j + 1) if j > i else i + 1


def _close_tail(s: str) -> str:
    """补齐缺失的闭合符（含**错位**：`]` 关不掉还开着的 `{`）。

    为什么需要：模型偶尔在 `chips` 里少写一个 `}`（实测 eth 1735 轮），
    于是 `"meta":{...}}]` 变成「`meta` 关了、chip 没关，`]` 直接跟上」。
    **花括号总数看起来没少**（外层最后那个 `}` 把窟窿补上了），
    所以 `_iter_json_objects` 会切出一个花括号平衡、方括号错位的片段，
    `_repair_json` / 截断恢复三条路全失效 → 整轮 parse_failed 报废。

    做法：单遍扫描，遇到对不上的闭合符就先把栈里更高的层级补掉
    （这就是「少写一个 `}`」的唯一形态），扫描完再把剩下的补齐。
    **只动结构、不动内容**：截断在字符串中间的一律原样返回，
    交给上面的「退到最后一个逗号」路径（那里会丢掉半个字段）。
    """
    t = (s or "").strip()
    if not t:
        return t
    out: list[str] = []
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in t:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        if ch in "{[":
            stack.append(ch)
            out.append(ch)
            continue
        if ch in "}]":
            want = "{" if ch == "}" else "["
            if want not in stack:
                continue  # 多出来的闭合符，丢掉
            while stack and stack[-1] != want:
                out.append("}" if stack.pop() == "{" else "]")
            stack.pop()
            out.append(ch)
            continue
        out.append(ch)
    if in_str:
        return t
    while stack:
        out.append("}" if stack.pop() == "{" else "]")
    return "".join(out)


def _looks_like_plan(obj) -> bool:
    return isinstance(obj, dict) and ("chips" in obj or "cycle_id" in obj or "reasoning" in obj)


def _extract_json_object(text: str):
    """从 LLM 输出提取 Plan JSON。

    容错：Markdown 围栏 / 尾逗号 / 单引号 / 注释 / 截断 / **多个 JSON 对象**
    （模型有时先吐一个 triggers 片段再吐主 Plan——优先选像 Plan 的那个）。
    """
    import json

    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()

    # 1) 整段直接解析
    for candidate in (t, _repair_json(t)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 1.5) 尾部少闭合符（多为 chips 里漏一个 `}`）：补上再试。
    #      必须排在「扫描平衡对象」之前 —— 缺 `}` 的文本切出来的片段
    #      花括号是平衡的（外层 `}` 顶替了 chip 的），下面三条路径都会失败。
    closed = _close_tail(t)
    if closed != t:
        for candidate in (closed, _repair_json(closed)):
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj

    # 2) 扫描所有平衡对象：先收像 Plan 的，再兜底第一个能解析的
    plan_like = []
    others = []
    for chunk in _iter_json_objects(t):
        for candidate in (chunk, _repair_json(chunk)):
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if _looks_like_plan(obj):
                plan_like.append(obj)
            else:
                others.append(obj)
            break
    if plan_like:
        return plan_like[0]
    if others:
        return others[0]

    # 3) 截断恢复：退到最后一个逗号闭合
    start = t.find("{")
    if start < 0:
        raise PlanError("LLM output has no JSON object")
    last_good = -1
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth > 0 and ch == ",":
            last_good = i
    if last_good > start:
        chunk = t[start:last_good] + "}]}"
        for candidate in (chunk, _repair_json(chunk)):
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise PlanError("LLM output has no valid JSON object")


def parse_plan_text(text: str, symbols: Optional[list[str]] = None) -> Plan:
    import json

    data = _extract_json_object(text)
    return parse_plan(data, symbols)
