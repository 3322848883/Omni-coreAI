#!/usr/bin/env python3
"""看 martin-grid 最新一轮的 AI 思考过程：reasoning_chain / tool_usage / 是否用到 BB。"""
import glob
import json
import os

BOT = "/opt/omnialpha/data/bots/martin-grid"
files = sorted(glob.glob(f"{BOT}/state/*thinking.json"), key=os.path.getmtime)
f = files[-1]
d = json.load(open(f, encoding="utf-8"))
print("file:", os.path.basename(f))
print("keys:", list(d.keys()))

chain = d.get("reasoning_chain") or []
print(f"\n=== reasoning_chain（{len(chain)} 条）===")
for i, step in enumerate(chain):
    if isinstance(step, dict):
        text = step.get("reasoning") or step.get("text") or json.dumps(step, ensure_ascii=False)
    else:
        text = str(step)
    print(f"[{i}] {text[:400]}")
    print()

print("=== tool_usage_summary ===")
print(json.dumps(d.get("tool_usage_summary"), ensure_ascii=False, indent=1)[:800])

tools = d.get("tool_usage") or []
print(f"\n=== tool_usage（{len(tools)} 次调用）===")
for t in tools[-10:]:
    if isinstance(t, dict):
        print(" ", t.get("name") or t.get("tool"), "->",
              json.dumps(t.get("args") or {}, ensure_ascii=False)[:160])
    else:
        print(" ", str(t)[:160])

head = d.get("content_head") or ""
print("\n=== content_head（最终回答开头）===")
print(head[:500])

# BB 相关性
blob = json.dumps(d, ensure_ascii=False)
print("\n=== BB 出现次数 ===")
for kw in ("boll20", "boll_upper", "boll_lower", "布林", "带宽", "BB"):
    print(f"  {kw}: {blob.count(kw)}")
