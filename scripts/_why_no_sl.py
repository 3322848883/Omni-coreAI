#!/usr/bin/env python3
"""看 15:50/15:55 两轮的 reasoning：为什么没补挂缺失的 SL。"""
import glob
import json
import os

BOT = "/opt/omnialpha/data/bots/martin-grid"
files = sorted(glob.glob(f"{BOT}/state/*thinking.json"), key=os.path.getmtime)[-2:]
for f in files:
    d = json.load(open(f, encoding="utf-8"))
    print("=" * 20, os.path.basename(f))
    for i, s in enumerate(d.get("reasoning_chain") or []):
        t = (s.get("reasoning") if isinstance(s, dict) else str(s)) or ""
        print(f"[{i}] {t[:420]}")
        print()
    head = d.get("content_head") or ""
    print("content_head:", head[:300])
    print()
