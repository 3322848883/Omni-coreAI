# -*- coding: utf-8 -*-
"""从 thinking.json 读三人本轮工具用量。"""
import json
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\gate-signal-bot")
for bot in ["pa-a", "smc-paper", "orderflow-paper"]:
    st = ROOT / "data" / "bots" / bot / "state"
    files = sorted(st.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"{bot}: 无")
        continue
    d = json.loads(files[0].read_text(encoding="utf-8"))
    s = d.get("tool_usage_summary") or {}
    usage = d.get("tool_usage") or []
    print(f"\n=== {bot} ({files[0].name}) ===")
    print(f"  工具 {s.get('total_calls', len(usage))} 次  查数据={s.get('data_checked')}")
    print(f"  {s.get('counts')}")
    for u in usage[:15]:
        print(f"    · {u.get('tool')} {u.get('args')} → {str(u.get('result_preview',''))[:70]}")
