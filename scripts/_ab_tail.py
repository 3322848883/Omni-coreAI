"""输出 A/B 结论段。"""
import json
from pathlib import Path

files = sorted(Path("data/bots/skill-ab/state").glob("*.thinking.json"),
               key=lambda p: p.stat().st_mtime)
for i, p in enumerate(files):
    d = json.loads(p.read_text(encoding="utf-8"))
    chain = d.get("reasoning_chain") or []
    label = "A 无skill" if i == 0 else "B 有skill"
    total = len("\n".join(chain))
    print("=" * 70)
    print(f"{label}  cycle={d.get('cycle_id')}  rounds={len(chain)}  total={total} 字")
    print("=" * 70)
    print(chain[-1][-1800:])
    print()
