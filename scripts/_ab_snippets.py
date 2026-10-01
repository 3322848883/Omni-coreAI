"""抽取 B 组（有 skill）体现方法论的片段。"""
import json
from pathlib import Path

p = sorted(Path("data/bots/skill-ab/state").glob("*.thinking.json"),
           key=lambda x: x.stat().st_mtime)[1]
d = json.loads(p.read_text(encoding="utf-8"))
chain = d.get("reasoning_chain") or []
full = "\n".join(chain)

import re
terms = ["Always In", "H1", "H2", "Brooks", "leg", "pullback", "breakout",
         "micro channel", "signal bar", "EMA20", "sweep", "range", "trend"]
print("B 组方法论术语上下文（每条截 160 字）:")
for t in terms:
    for m in list(re.finditer(re.escape(t), full))[:2]:
        s = max(0, m.start() - 60)
        print(f"  [{t}] …{full[s:m.start()+100]}…".replace("\n", " "))
    print()
