"""多人格共管订单（multi-persona）—— N 个策略人格共同管理订单。

拓扑：
  single_account   N 人格 → 1 账户一单（去重单执行）
  mirror_accounts  N 人格 → N 账户同步开平（复用 broadcast 分发）

决策：各人格独立分析 → 融合层（weighted_vote|master_arbiter|consensus）
共同记忆：data/shared/orders/<order_id>.json 跨 bot 读写
"""
from __future__ import annotations

from .config import (
    PersonaGroup,
    PersonaError,
    load_persona_groups,
    validate_group,
)
from .fusion import fuse_plans
from .orders import SharedOrderStore
from .runner import PersonaRunner

__all__ = [
    "PersonaGroup",
    "PersonaError",
    "PersonaRunner",
    "SharedOrderStore",
    "fuse_plans",
    "load_persona_groups",
    "validate_group",
]
