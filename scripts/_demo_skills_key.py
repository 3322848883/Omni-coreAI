#!/usr/bin/env python3
"""演示 skills 键三种取值的实际差异。"""
import sys
from pathlib import Path

ROOT = Path("/opt/gate-signal-bot")
sys.path.insert(0, str(ROOT))
from gate_bot.skillkit import SkillRegistry, render_catalog  # noqa: E402

reg = SkillRegistry()
reg.scan([ROOT / ".mimocode" / "skills", ROOT / "skills"])
print("已装 skill:", reg.ids())
print()

cases = [
    ("缺省（当前 brooks-btc 的状态）", None),
    ("显式锁定 [price-action-trading]", ["price-action-trading"]),
    ("显式关闭 []", []),
]
for label, val in cases:
    vis = reg.visible_for("brooks-btc", val)
    cat = render_catalog(vis)
    print(f"--- {label} ---")
    print(f"   模型可见 skill: {[m.id for m in vis]}")
    print(f"   catalog 字符数: {len(cat)}")
    if cat:
        first = cat.splitlines()[1] if len(cat.splitlines()) > 1 else ""
        print(f"   catalog 首行: {first[:70]}")
    print()
