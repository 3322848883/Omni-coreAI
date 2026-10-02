#!/usr/bin/env python3
"""给服务器实盘 brooks-btc 的 overlay 加 skill 白名单（显式锁定）。"""
import shutil
from pathlib import Path

OV = Path("/opt/omnialpha/config/bots.local/brooks-btc.yaml")

print("=== 改前 ===")
print(OV.read_text(encoding="utf-8"))

shutil.copy2(OV, OV.with_suffix(".yaml.bak"))

OV.write_text(
    "# 服务器 overlay：启用 brooks-btc + 显式锁定 skill 白名单\n"
    "# 基线 config/bots/brooks-btc.yaml 为 enabled: false\n"
    "enabled: true\n"
    "strategist:\n"
    "  skills: [price-action-trading]   # 白名单：只有这个 skill 对实盘可见\n"
    "                                    # 新装 skill 不会自动进实盘（发布≠可用）\n",
    encoding="utf-8",
)

print("=== 改后 ===")
print(OV.read_text(encoding="utf-8"))

# 校验生效
import sys  # noqa: E402

sys.path.insert(0, "/opt/omnialpha")
from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.skillkit import SkillRegistry, render_catalog  # noqa: E402

b = load_bot_config(Path("/opt/omnialpha/config/bots/brooks-btc.yaml"))
print("=== 校验 ===")
print("  bot.enabled:", b.enabled)
print("  strategist.skills:", (b.strategist or {}).get("skills"))

reg = SkillRegistry()
reg.scan([Path("/opt/omnialpha/skills")])
vis = reg.visible_for(b.bot_id, (b.strategist or {}).get("skills"))
print("  实盘可见 skill:", [m.id for m in vis])
print("  catalog 长度:", len(render_catalog(vis)))
