#!/usr/bin/env python3
"""查最新一轮：工具调用是否含 indicators/sqzmom + reasoning 首句是否报挤压。"""
import glob
import json
import os
import re

BOT = "/opt/omnialpha/data/bots/martin-grid"
f = max(glob.glob(f"{BOT}/state/*thinking.json"), key=os.path.getmtime)
d = json.load(open(f, encoding="utf-8"))
print("file:", os.path.basename(f))
tu = d.get("tool_usage_summary") or {}
print("tools:", json.dumps(tu.get("counts"), ensure_ascii=False), "total:", tu.get("total_calls"))
for t in (d.get("tool_usage") or [])[-8:]:
    if isinstance(t, dict):
        print("  call:", t.get("name") or t.get("tool"), "->",
              json.dumps(t.get("args") or {}, ensure_ascii=False)[:120])
head = d.get("content_head") or ""
m = re.search(r'"reasoning":\s*"([^"]*)"', head)
print("\n最终 reasoning:", (m.group(1) if m else head[:200])[:220])
blob = json.dumps(d, ensure_ascii=False)
print("挤压提及:", blob.count("挤压"), "| sqz 提及:", blob.lower().count("sqz"))
