"""统计最新 A/B 两组的术语密度与规模。"""
import json
from pathlib import Path

files = sorted(Path("data/bots/skill-ab/state").glob("*.thinking.json"),
               key=lambda p: p.stat().st_mtime)[-2:]
terms = ["Always In", "Always-In", "H1", "H2", "Brooks", "leg", "pullback",
         "breakout", "signal bar", "climax", "BAN", "trader", "Trader",
         "measuring", "micro channel", "barbwire", "range", "trend"]
a = files[0].read_text(encoding="utf-8")
b = files[1].read_text(encoding="utf-8")
print(f"{'术语':<18}{'A(无skill)':>12}{'B(有skill)':>12}")
for t in terms:
    print(f"{t:<18}{a.count(t):>12}{b.count(t):>12}")
print()
print(f"thinking 字符:  A = {len(a)}   B = {len(b)}")
for i, p in enumerate(files):
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"  file{i}: {p.name}  rounds={len(d.get('reasoning_chain') or [])}")

# 保存最终对比
out = Path("verify_data/skill-ab-comparison.md")
blocks = []
for i, p in enumerate(files):
    d = json.loads(p.read_text(encoding="utf-8"))
    chain = d.get("reasoning_chain") or []
    label = "A（无 skill）" if i == 0 else "B（有 skill）"
    blocks.append((label, d.get("cycle_id"), len(chain), len("\n".join(chain)), "\n\n".join(chain)))
print()
for label, cyc, r, n, _ in blocks:
    print(f"{label}: cycle={cyc} rounds={r} chars={n}")
