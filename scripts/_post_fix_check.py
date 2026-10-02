# -*- coding: utf-8 -*-
"""弹窗修复后全面体检。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omnialpha.watchdog import Watchdog  # noqa: E402

root = Path(r"C:\Users\w6485\Desktop\测试\OmniAlpha")
wd = Watchdog(root, notify=False)
wd.discover()
print("目标组件:", len(wd.targets), "bot 数:", len({t.bot_id for t in wd.targets}))
r = wd.check_once()
print("在跑:", len(r["held"]), " 缺失:", len(r["missing"]), " 本次拉起:", len(r["restarted"]))
if r["missing"]:
    print("缺失:", r["missing"])
