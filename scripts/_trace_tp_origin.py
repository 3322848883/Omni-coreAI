#!/usr/bin/env python3
"""找 15:33 轮（cycle 3750）的 reasoning：TP 2364.5 第一次挂出时的完整依据。"""
import json
import glob

for pat in ("/opt/omnialpha/data/bots/martin-grid/state/20261027-153349-*thinking.json",
            "/opt/omnialpha/data/bots/martin-grid/state/20261027-1533*"):
    fs = sorted(glob.glob(pat))
    if fs:
        f = fs[-1]
        break
else:
    f = None
print("thinking:", f)
if f:
    d = json.load(open(f, encoding="utf-8"))
    for i, s in enumerate(d.get("reasoning_chain") or []):
        t = (s.get("reasoning") if isinstance(s, dict) else str(s)) or ""
        if "2364" in t or "TP" in t or "tp" in t:
            print(f"[{i}] {t[:500]}")
            print()
    head = d.get("content_head") or ""
    import re
    m = re.search(r".{0,60}2364\.5.{0,180}", head)
    print("content_head 中 2364.5 上下文：", m.group(0)[:240] if m else head[:240])

# archive 里的 meta.plan_reasoning
import glob as g
for c in g.glob("/opt/omnialpha/data/bots/martin-grid/archive/*/*3750*"):
    if c.endswith(".result.json"):
        continue
    d2 = json.load(open(c, encoding="utf-8"))
    meta = d2.get("meta") or {}
    print("\narchive 最终 reasoning:", (meta.get("plan_reasoning") or "")[:240])
