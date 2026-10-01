# -*- coding: utf-8 -*-
"""查三人分析的币种 + 跑 BTC/ETH/SOL 三轮。"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\gate-signal-bot")
os.chdir(ROOT)
os.environ["GATE_BOT_ROOT"] = str(ROOT)
sb = ROOT / "scripts" / "secrets.bat"
if sb.exists():
    for line in sb.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("if ") and "set " in line:
            kv = line.split("set ", 1)[-1].strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT))
from gate_bot.config import load_all_bots  # noqa: E402

bots = load_all_bots(ROOT / "config" / "bots")
print("=== 各人格分析的币种 ===")
for bid in ["pa-a", "smc-paper", "orderflow-paper"]:
    b = bots.get(bid)
    print(f"  {bid}: {getattr(b, 'symbols', None)}")
