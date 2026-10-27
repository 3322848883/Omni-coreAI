#!/usr/bin/env python3
"""看带宽在最新一轮推理中的具体用法 + 最终 reasoning。"""
import glob
import json
import os
import re

BOT = "/opt/omnialpha/data/bots/martin-grid"
f = max(glob.glob(f"{BOT}/state/*thinking.json"), key=os.path.getmtime)
d = json.load(open(f, encoding="utf-8"))
blob = json.dumps(d, ensure_ascii=False)

print("=== 带宽 出现的上下文 ===")
for m in re.finditer(r".{0,60}带宽.{0,140}", blob):
    print("·", m.group(0)[:200].replace("\\n", " "))
    print()

print("=== 最终 reasoning（archive done）===")
for pat in ("archive/done/*.json", "archive/*/*.json"):
    cands = [x for x in glob.glob(f"{BOT}/{pat}") if "c-eth-20261025T3720" in x]
    if cands:
        d2 = json.load(open(sorted(cands)[-1], encoding="utf-8"))
        meta = d2.get("meta") or {}
        r = meta.get("plan_reasoning") or ""
        print(r[:260])
        chips = d2.get("orders") or []
        print("orders:", [(o.get("action"), o.get("price"), o.get("size_usd")) for o in chips[:5]])
        break
