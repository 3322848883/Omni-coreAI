#!/usr/bin/env python3
"""演示：新装第二个 skill 后，缺省 vs 显式锁定的差别。"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))
from gate_bot.skillkit import SkillRegistry, render_catalog  # noqa: E402

# 造一个临时 skills 目录：现有 skill + 一个"新装的"
td = Path(tempfile.mkdtemp())
(td / "price-action-trading").mkdir()
(td / "price-action-trading" / "SKILL.md").write_text(
    '---\nname: price-action-trading\ndescription: "价格行为分析。Use when 分析 K 线。"\n---\n\n# PA\n',
    encoding="utf-8",
)
# 新装一个（模拟第三方/实验 skill）
(td / "new-experiment").mkdir()
(td / "new-experiment" / "SKILL.md").write_text(
    '---\nname: new-experiment\ndescription: "某个新装实验 skill。Use when 测试新策略。"\n---\n\n# New\n',
    encoding="utf-8",
)

reg = SkillRegistry()
reg.scan([td])
print("已装 skill:", reg.ids())
print()

for label, val in [
    ("① 缺省（brooks-btc 当前状态）", None),
    ("② 显式锁定 [price-action-trading]", ["price-action-trading"]),
]:
    vis = reg.visible_for("brooks-btc", val)
    print(f"{label}")
    print(f"   → 实盘 bot 能看到: {[m.id for m in vis]}")
    print()

print("结论：新装 new-experiment 后——")
print("  ① 缺省：实盘 bot 自动看到新 skill（无 opt-in 闸门）")
print("  ② 锁定：实盘 bot 仍只见 price-action-trading（新 skill 进不来）")
