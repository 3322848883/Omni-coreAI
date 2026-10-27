#!/usr/bin/env python3
"""看 18:54 轮完整 reasoning：首句挤压声明 + 挤压相关的判断内容。"""
import glob
import json
import os
import re

BOT = "/opt/omnialpha/data/bots/martin-grid"
f = max(glob.glob(f"{BOT}/state/*thinking.json"), key=os.path.getmtime)
d = json.load(open(f, encoding="utf-8"))
print("file:", os.path.basename(f))

chain = d.get("reasoning_chain") or []
for i, s in enumerate(chain):
    t = (s.get("reasoning") if isinstance(s, dict) else str(s)) or ""
    if "挤压" in t or "sqz" in t.lower():
        print(f"\n[{i}] {t[:450]}")

head = d.get("content_head") or ""
m = re.search(r'"reasoning":\s*"([^"]*)"', head)
r = m.group(1) if m else ""
print("\n=== 最终 reasoning 全文 ===")
print(r[:400])
print("\n首句含挤压:", "挤压" in r[:60])
