#!/usr/bin/env python3
"""分析服务器上 brooks-btc 的 skill 使用情况。"""
import json
import re
from pathlib import Path

ROOT = Path("/opt/gate-signal-bot")
STATE = ROOT / "data" / "bots" / "brooks-btc" / "state"

th = sorted(STATE.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime)
if not th:
    print("(no thinking files)")
    raise SystemExit(0)

p = th[-1]
d = json.loads(p.read_text(encoding="utf-8"))
chain = d.get("reasoning_chain") or []
full = "\n".join(chain)

print(f"file:   {p.name}")
print(f"cycle:  {d.get('cycle_id')}")
print(f"rounds: {len(chain)}   chars: {len(full)}")
print()

print("术语命中:")
for t in ["skill", "Always In", "H2", "L2", "Brooks", "BAN", "barbwire",
          "trader", "climax", "sweep", "range", "pullback", "breakout"]:
    c = len(re.findall(re.escape(t), full))
    if c:
        print(f"  {t:<12} {c}")

print()
print("=== thinking 前 1500 字 ===")
print(full[:1500])

# 最终 plan
holds = sorted(STATE.glob("*.hold.json"), key=lambda x: x.stat().st_mtime)
inbox = sorted((ROOT / "data" / "bots" / "brooks-btc" / "inbox").glob("*.json"),
               key=lambda x: x.stat().st_mtime)
print()
print("=== 最终产物 ===")
if inbox:
    sig = json.loads(inbox[-1].read_text(encoding="utf-8"))
    print("SIGNAL:", json.dumps(sig, ensure_ascii=False)[:800])
elif holds:
    h = json.loads(holds[-1].read_text(encoding="utf-8"))
    print("HOLD:", json.dumps(h, ensure_ascii=False)[:800])
