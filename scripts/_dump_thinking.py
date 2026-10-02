# -*- coding: utf-8 -*-
"""提取三人格最新一轮的完整思考原文。"""
import json
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\OmniAlpha")

for bot in ["pa-a", "smc-paper", "orderflow-paper"]:
    st = ROOT / "data" / "bots" / bot / "state"
    files = sorted(st.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    print("=" * 70)
    print(f"### {bot}")
    print("=" * 70)
    if not files:
        print("(无记录)")
        continue
    f = files[0]
    d = json.loads(f.read_text(encoding="utf-8"))
    print(f"file: {f.name}")
    rc = d.get("reasoning_chain") or []
    total = sum(len(x) for x in rc)
    print(f"reasoning_chain 段数={len(rc)} 总字符={total}")
    for i, seg in enumerate(rc):
        print(f"\n--- 思考段 {i+1} ({len(seg)} 字符) ---")
        print(seg)
    ch = d.get("content_head") or ""
    print(f"\n--- 最终输出 content_head ({len(ch)} 字符) ---")
    print(ch)
    print()
