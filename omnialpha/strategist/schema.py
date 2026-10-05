"""Plan / chips schema for LLM strategist (multi-symbol decisions)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

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

# 契约 Tier 1：区域三选一（见 docs/compose/spec/pa-skills-upgrade.md [S2]）
REGIONS = {"trend", "range", "reversal"}

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


def _safe_symbol(symbol: str) -> str:
    import re
    from ..gate_client import resolve_symbol
    s = resolve_symbol(str(symbol or ''))
    if not re.fullmatch(r'[A-Z0-9_]{2,20}', s or ''):
        raise PlanError(f'symbol invalid: {symbol!r}')
    return s
    pass


@dataclass
class Chip:
    symbol: str
    action: str
    confidence: float = 0.0
    size_usd: Optional[float] = None
    size: Optional[int] = None
    tp: Optional[float] = None
    sl: Optional[float] = None
    tp2: Optional[float] = None  # 多级止盈
    tp3: Optional[float] = None
    tp1_share: Optional[float] = None
    tp2_share: Optional[float] = None
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
    rule_ids: list[str] = field(default_factory=list)
    scenarios: dict = field(default_factory=dict)

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
        return d


@dataclass
class Plan:
    cycle_id: str
    reasoning: str = ""
    chips: list[Chip] = field(default_factory=list)
    triggers: list = field(default_factory=list)
    trigger_ops: list = field(default_factory=list)
    rejected_triggers: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    return float(v)


def parse_plan(data: Any) -> Plan:
    if not isinstance(data, dict):
        raise PlanError("plan must be a JSON object")
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
        conf = float(raw.get("confidence") or 0)
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
        tp2 = _f(raw.get("tp2"))
        # 区域=区间 → 禁止 2R 目标。把提示词那条规则（「区间只做 scalp，
        # 禁止持有 2R 目标」）从一句话变成程序约束 —— 否则区域判定只是模型
        # 自己贴的标签，为用双止盈改判成趋势就绕过去了（实测发生过）。
        if region == "range" and tp2 is not None:
            raise PlanError(
                f"chips[{i}].tp2 must be empty when region=range "
                f"(区间只做 scalp，禁止持有 2R 目标)")
        rule_ids_raw = raw.get("rule_ids") or []
        if not isinstance(rule_ids_raw, list):
            raise PlanError(f"chips[{i}].rule_ids must be an array")
        scenarios_raw = raw.get("scenarios") or {}
        if not isinstance(scenarios_raw, dict):
            raise PlanError(f"chips[{i}].scenarios must be an object")
        chips.append(
            Chip(
                symbol=_safe_symbol(symbol),
                action=action,
                confidence=conf,
                size_usd=_f(raw.get("size_usd")),
                size=int(raw["size"]) if raw.get("size") is not None else None,
                tp=_f(raw.get("tp")),
                sl=_f(raw.get("sl")),
                tp2=tp2,
                tp3=_f(raw.get("tp3")),
                tp1_share=_f(raw.get("tp1_share")),
                tp2_share=_f(raw.get("tp2_share")),
                order_type=order_type,
                price=_f(raw.get("price")),
                trigger_price=_f(raw.get("trigger_price")),
                leverage=int(raw["leverage"]) if raw.get("leverage") is not None else None,
                side=side,
                tp_mode="limit_order" if tp_mode == "limit" else tp_mode,
                sl_mode="limit_order" if sl_mode == "limit" else sl_mode,
                reasoning=str(raw.get("reasoning") or ""),
                region=region,
                invalidation=_f(raw.get("invalidation")),
                time_stop_bars=(int(raw["time_stop_bars"])
                                if raw.get("time_stop_bars") is not None else None),
                give_back_pct=_f(raw.get("give_back_pct")),
                risk_pct=_f(raw.get("risk_pct")),
                rule_ids=[str(x) for x in rule_ids_raw],
                scenarios=dict(scenarios_raw),
            )
        )
    triggers = data.get("triggers") or []
    if not isinstance(triggers, list):
        raise PlanError("triggers must be an array")
    trigger_ops = data.get("trigger_ops") or []
    if not isinstance(trigger_ops, list):
        raise PlanError("trigger_ops must be an array")
    return Plan(
        cycle_id=str(data.get("cycle_id") or ""),
        reasoning=str(data.get("reasoning") or ""),
        chips=chips,
        triggers=triggers,
        trigger_ops=trigger_ops,
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


def parse_plan_text(text: str) -> Plan:
    import json

    data = _extract_json_object(text)
    return parse_plan(data)
