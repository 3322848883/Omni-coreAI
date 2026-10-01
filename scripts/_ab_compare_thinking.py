"""对比 A/B 两组的 thinking 分析链。"""
import json
from pathlib import Path

STATE = Path("data/bots/skill-ab/state")
files = sorted(STATE.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime)
print(f"thinking 文件数: {len(files)}")
for f in files:
    d = json.loads(f.read_text(encoding="utf-8"))
    chain = d.get("reasoning_chain") or []
    full = "\n".join(chain)
    print(f"\n{'='*60}\n{f.name}")
    print(f"  cycle: {d.get('cycle_id')}  rounds: {len(chain)}  chars: {len(full)}")
    for i, c in enumerate(chain, 1):
        print(f"  -- round {i} ({len(c)} chars) --")
        print("   ", c[:200].replace("\n", " "))

# 输出对比全文
out = Path("verify_data/skill-ab-thinking.md")
blocks = []
for f in files:
    d = json.loads(f.read_text(encoding="utf-8"))
    chain = d.get("reasoning_chain") or []
    label = "A（无 skill）" if len(blocks) == 0 else "B（有 skill）"
    blocks.append(f"## {label} — {d.get('cycle_id')}\n\n轮数 {len(chain)}，共 {len(chr(10).join(chain))} 字符\n\n"
                  + "\n\n".join(f"### round {i}\n\n{c}" for i, c in enumerate(chain, 1)))
out.write_text("# A/B thinking 分析链对比\n\n" + "\n\n---\n\n".join(blocks), encoding="utf-8")
print(f"\n保存: {out}")
