# -*- coding: utf-8 -*-
"""汇总三人最近 3 轮工具用量。"""
import json
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\gate-signal-bot")
for bot in ["pa-a", "smc-paper", "orderflow-paper"]:
    st = ROOT / "data" / "bots" / bot / "state"
    fs = sorted(st.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:4]
    print(f"\n=== {bot} ===")
    for f in fs:
        d = json.loads(f.read_text(encoding="utf-8"))
        s = d.get("tool_usage_summary") or {}
        print(f"  {f.name[:32]}  工具={s.get('total_calls', 0)}  查数据={s.get('data_checked')}  {s.get('counts')}")
