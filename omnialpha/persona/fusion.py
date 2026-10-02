"""决策融合：weighted_vote / master_arbiter / consensus。

输入：各人格 Plan（decision + confidence + tp/sl 等）
输出：一个执行动作（或 hold）
"""
from __future__ import annotations

from typing import Any, Optional

from .config import PersonaGroup, member_weight

# 决策归一到这些方向/动作类
DIR_LONG = "long"
DIR_SHORT = "short"
DIR_HOLD = "hold"
DIR_CLOSE = "close"       # close / close_all / flatten
DIR_REDUCE = "reduce"     # reduce_long / reduce_short
DIR_MODIFY = "modify"     # modify_tp_sl

MANAGE_DIRS = (DIR_CLOSE, DIR_REDUCE, DIR_MODIFY)


def _norm_dir(plan: dict) -> str:
    """从 Plan 的 chips/decision 提取方向或管理动作类。

    注意：管理动作（close/reduce/modify）是独立决策类，
    不可折叠为 hold，否则共管仓位无法平仓/减仓/改单。
    """
    d = str(plan.get("decision") or plan.get("direction") or "").lower()
    chips = plan.get("chips") or []
    act = ""
    if chips and isinstance(chips, list):
        act = str((chips[0] or {}).get("action") or "").lower()
    act = act or d

    if act in ("close", "close_all", "flatten", "close_long", "close_short"):
        return DIR_CLOSE
    if act in ("reduce_long", "reduce_short"):
        return DIR_REDUCE
    if act == "modify_tp_sl":
        return DIR_MODIFY
    if act in ("long", "open_long", "add_long", "stop_entry_long"):
        return DIR_LONG
    if act in ("short", "open_short", "add_short", "stop_entry_short"):
        return DIR_SHORT
    return DIR_HOLD


def _norm_confidence(plan: dict) -> float:
    try:
        c = plan.get("confidence")
        if c is None:
            chips = plan.get("chips") or []
            if chips and isinstance(chips, list):
                c = (chips[0] or {}).get("confidence")
        return max(0.0, min(1.0, float(c or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def fuse_plans(
    group: PersonaGroup,
    plans: dict[str, dict],
    on_conflict: Optional[str] = None,
) -> dict:
    """按组的 fusion 模式合并各人格 Plan。

    plans: {bot_id: plan_dict}
    返回 {decision, action, confidence, votes, mode, reason, bot_id(裁决者)}
    decision ∈ long|short|hold|close|reduce|modify
    action  = 具体执行动作（open_long/close/reduce_long/modify_tp_sl/hold...）
    """
    mode = group.fusion
    conflict = (on_conflict or group.on_conflict or "hold").lower()
    votes = {b: _norm_dir(p) for b, p in plans.items()}
    confs = {b: _norm_confidence(p) for b, p in plans.items()}

    if mode == "master_arbiter":
        return _fuse_master(group, plans, votes, confs, conflict)
    if mode == "consensus":
        return _fuse_consensus(group, plans, votes, confs)
    return _fuse_weighted(group, plans, votes, confs, conflict)


def _action_of(plan: dict, fallback: str = "hold") -> str:
    chips = plan.get("chips") or []
    if chips and isinstance(chips, list) and (chips[0] or {}).get("action"):
        return str(chips[0]["action"])
    return fallback


def _fuse_weighted(group, plans, votes, confs, conflict) -> dict:
    """加权投票：各决策类（含管理动作）权重过半 → 执行。"""
    buckets = {DIR_LONG: 0.0, DIR_SHORT: 0.0, DIR_HOLD: 0.0,
               DIR_CLOSE: 0.0, DIR_REDUCE: 0.0, DIR_MODIFY: 0.0}
    for b, d in votes.items():
        w = member_weight(group, b)
        if d in buckets:
            buckets[d] += w
    total = sum(buckets.values()) or 1.0
    decision = DIR_HOLD
    for d, w in buckets.items():
        if w > total / 2:
            decision = d
            break
    if decision == DIR_HOLD and (buckets[DIR_LONG] > 0 and buckets[DIR_SHORT] > 0):
        # 方向冲突
        if conflict == "majority":
            decision = DIR_LONG if buckets[DIR_LONG] >= buckets[DIR_SHORT] else DIR_SHORT
        elif conflict == "master":
            master = group.fusion_config.get("master") or group.members[0]
            decision = votes.get(master, DIR_HOLD)
        else:
            decision = DIR_HOLD

    # 取该决策类中权重最高成员的具体 action
    action = "hold"
    best_w = -1.0
    for b, d in votes.items():
        if d == decision:
            w = member_weight(group, b)
            if w > best_w:
                best_w = w
                action = _action_of(plans.get(b) or {}, fallback=_default_action(decision))
    conf = max(confs.values()) if confs else 0.0
    return {
        "decision": decision, "action": action, "confidence": conf,
        "votes": votes, "mode": "weighted_vote",
        "reason": " ".join(f"{d}={w:.1f}" for d, w in buckets.items()),
    }


def _default_action(decision: str) -> str:
    return {
        DIR_LONG: "open_long", DIR_SHORT: "open_short",
        DIR_CLOSE: "close", DIR_REDUCE: "reduce_long",
        DIR_MODIFY: "modify_tp_sl", DIR_HOLD: "hold",
    }.get(decision, "hold")


def _fuse_master(group, plans, votes, confs, conflict) -> dict:
    """主人格裁决，其他 Plan 作参考。"""
    master = str(group.fusion_config.get("master") or group.members[0])
    decision = votes.get(master, DIR_HOLD)
    action = _action_of(plans.get(master) or {}, fallback=_default_action(decision))
    conf = confs.get(master, 0.0)
    return {
        "decision": decision, "action": action, "confidence": conf,
        "votes": votes, "mode": "master_arbiter", "master": master,
        "reason": f"master {master} → {decision}/{action}; others={votes}",
    }


def _fuse_consensus(group, plans, votes, confs) -> dict:
    """共识分：方向一致加分、分歧减分（confidence 加权）；≥阈值才执行。"""
    thr = float(group.fusion_config.get("threshold", 0.7))
    buckets: dict[str, float] = {}
    for b, d in votes.items():
        w = member_weight(group, b) * (0.5 + 0.5 * confs.get(b, 0.0))
        buckets[d] = buckets.get(d, 0.0) + w
    total = sum(buckets.values())
    if total <= 0:
        return {"decision": DIR_HOLD, "action": "hold", "confidence": 0.0,
                "votes": votes, "mode": "consensus", "reason": "no directional votes"}
    top, top_w = max(buckets.items(), key=lambda kv: kv[1])
    score = top_w / total
    decision = top if score >= thr else DIR_HOLD
    action = "hold"
    if decision != DIR_HOLD:
        best_w = -1.0
        for b, d in votes.items():
            if d == decision:
                w = member_weight(group, b) * (0.5 + 0.5 * confs.get(b, 0.0))
                if w > best_w:
                    best_w = w
                    action = _action_of(plans.get(b) or {}, fallback=_default_action(decision))
    return {
        "decision": decision, "action": action, "confidence": score,
        "votes": votes, "mode": "consensus",
        "reason": f"consensus {top}={score:.2f} thr={thr}",
    }
