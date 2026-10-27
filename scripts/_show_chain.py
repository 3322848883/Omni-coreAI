#!/usr/bin/env python3
"""19:29 轮完整 reasoning_chain：为什么只挂 3 条腿。"""
import glob
import json
import os

f = max(glob.glob("/opt/omnialpha/data/bots/martin-grid/state/*thinking.json"),
        key=os.path.getmtime)
print(os.path.basename(f))
d = json.load(open(f, encoding="utf-8"))
for i, s in enumerate(d.get("reasoning_chain") or []):
    t = (s.get("reasoning") if isinstance(s, dict) else str(s)) or ""
    print(f"\n[{i}] {t[:700]}")
