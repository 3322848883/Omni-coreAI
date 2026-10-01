#!/usr/bin/env python3
"""验证服务器 skill：W05 是否消失 + 原文分卷可读。"""
import sys
from pathlib import Path

sys.path.insert(0, "/opt/gate-signal-bot")
from gate_bot.skillkit import SkillRegistry, run_skill_ref, validate_package  # noqa: E402

ROOT = Path("/opt/gate-signal-bot")
SKILL = ROOT / "skills" / "price-action-trading"

print("=== validate ===")
rep = validate_package(SKILL)
print(rep.summary())

print()
print("=== skill_ref 读原文分卷 ===")
reg = SkillRegistry()
reg.scan([ROOT / "skills"])
for path in (
    "references/knowledge/source/V1_趋势篇/V1_B01_P001-050.md",
    "references/knowledge/source/V2_区间/V2_B01_P001-050.md",
    "references/knowledge/source/V3_反转/V3_B01_P001-050.md",
):
    try:
        out = run_skill_ref(reg, {"name": "price-action-trading", "path": path},
                            bot_id="verify", enabled_ids=["price-action-trading"], root=ROOT)
        print(f"  OK  {path}  ({len(out)} 字符)")
    except Exception as e:
        print(f"  ERR {path}: {e}")

print()
print("=== bundled 文件总数 ===")
pkg = reg.get_package("price-action-trading")
print("  ", len(pkg.files), "个")
