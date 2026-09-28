"""persona_groups.yaml 配置：拓扑 / 融合模式 / 成员。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

FUSION_MODES = ("weighted_vote", "master_arbiter", "consensus")
TOPOLOGIES = ("single_account", "mirror_accounts")
CONFLICT_MODES = ("hold", "master", "majority")


class PersonaError(Exception):
    pass


@dataclass
class PersonaGroup:
    name: str
    members: list[str]
    topology: str = "single_account"
    target_account: str = ""           # single_account 时订单落在谁
    fusion: str = "weighted_vote"
    fusion_config: dict = field(default_factory=dict)
    on_conflict: str = "hold"
    trigger: dict = field(default_factory=dict)   # 可选组级触发覆盖
    enabled: bool = True


def _check_bot_id(bot_id: str, role: str) -> str:
    bid = str(bot_id or "").strip()
    if not bid:
        raise PersonaError(f"{role} bot_id required")
    if "/" in bid or "\\" in bid or bid in (".", "..") or ".." in bid:
        raise PersonaError(f"{role} invalid bot_id {bid!r}")
    return bid


def load_persona_groups(path: Path) -> list[PersonaGroup]:
    p = Path(path)
    if not p.exists():
        raise PersonaError(f"persona groups config not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise PersonaError(f"persona config must be a mapping: {p}")
    groups = []
    for i, g in enumerate(data.get("groups") or []):
        if not isinstance(g, dict):
            raise PersonaError(f"groups[{i}] must be a mapping")
        name = str(g.get("name") or "").strip() or f"group{i}"
        members = [str(x).strip() for x in (g.get("members") or []) if str(x).strip()]
        if len(members) < 2:
            raise PersonaError(f"group {name!r}: members must list >=2 bots")
        topology = str(g.get("topology") or "single_account").strip().lower()
        if topology not in TOPOLOGIES:
            raise PersonaError(f"group {name!r}: topology must be one of {TOPOLOGIES}")
        fusion = str(g.get("fusion") or "weighted_vote").strip().lower()
        if fusion not in FUSION_MODES:
            raise PersonaError(f"group {name!r}: fusion must be one of {FUSION_MODES}")
        on_conflict = str(g.get("on_conflict") or "hold").strip().lower()
        if on_conflict not in CONFLICT_MODES:
            raise PersonaError(f"group {name!r}: on_conflict must be one of {CONFLICT_MODES}")
        target = str(g.get("target_account") or "").strip()
        if topology == "single_account":
            # 未指定则取第一个成员
            target = target or members[0]
            if target not in members:
                raise PersonaError(f"group {name!r}: target_account {target!r} not in members")
        groups.append(PersonaGroup(
            name=name,
            members=members,
            topology=topology,
            target_account=target,
            fusion=fusion,
            fusion_config=dict(g.get("fusion_config") or {}),
            on_conflict=on_conflict,
            trigger=dict(g.get("trigger") or {}),
            enabled=bool(g.get("enabled", True)),
        ))
    return groups


def validate_group(group: PersonaGroup, known_bots: set[str]) -> None:
    if not group.enabled:
        return
    for b in group.members:
        _check_bot_id(b, f"group {group.name!r} member")
        if b not in known_bots:
            raise PersonaError(
                f"group {group.name!r}: member bot not found: {b!r} (known: {sorted(known_bots)})"
            )
    if group.topology == "single_account":
        if group.target_account not in group.members:
            raise PersonaError(f"group {group.name!r}: target_account not in members")
    # weighted_vote 需 weights 覆盖成员
    if group.fusion == "weighted_vote":
        weights = group.fusion_config.get("weights") or {}
        missing = [b for b in group.members if b not in weights]
        if missing:
            # 默认权重 1.0，允许缺失
            pass
    if group.fusion == "master_arbiter":
        master = str(group.fusion_config.get("master") or "")
        if master and master not in group.members:
            raise PersonaError(f"group {group.name!r}: master {master!r} not in members")
    if group.fusion == "consensus":
        thr = float(group.fusion_config.get("threshold", 0.7))
        if not (0 < thr <= 1):
            raise PersonaError(f"group {group.name!r}: consensus threshold must be in (0,1]")


def member_weight(group: PersonaGroup, bot_id: str) -> float:
    w = (group.fusion_config.get("weights") or {}).get(bot_id)
    try:
        return float(w) if w is not None else 1.0
    except (TypeError, ValueError):
        return 1.0
