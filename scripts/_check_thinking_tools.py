# -*- coding: utf-8 -*-
"""查看 thinking.json 的工具调用结构。"""
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\OmniAlpha")

for bot in ["smc-paper", "orderflow-paper", "pa-a"]:
    files = sorted((ROOT / "data" / "bots" / bot / "state").glob("*.thinking.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"\n=== {bot}: 无 thinking 记录 ===")
        continue
    d = json.loads(files[0].read_text(encoding="utf-8"))
    print(f"\n=== {bot} ({files[0].name}) ===")
    print("顶层字段:", list(d.keys()))
    rc = d.get("reasoning_chain") or []
    print(f"reasoning_chain 段数: {len(rc)}")
    # 找工具相关
    for k, v in d.items():
        if any(x in k.lower() for x in ("tool", "call", "data", "klines", "indicator")):
            preview = str(v)[:200]
            print(f"  [{k}] {type(v).__name__}: {preview}")
