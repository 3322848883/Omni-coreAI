"""分析全方位测试的 LLM 工具使用与产出。"""
import json
import re
from pathlib import Path

STATE = Path("data/bots/skill-e2e/state")
th = sorted(STATE.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime)[-1]
d = json.loads(th.read_text(encoding="utf-8"))
chain = d.get("reasoning_chain") or []
full = "\n".join(chain)

print(f"cycle: {d.get('cycle_id')}  rounds: {len(chain)}  chars: {len(full)}")
print()

TOOLS = ["skill", "skill_ref", "klines", "indicators", "ticker", "orderbook", "contract",
         "stats", "account", "trades_flow", "liquidations", "market_stats", "tech_analysis",
         "coin_info", "onchain", "social", "overview", "sentiment", "macro",
         "smc_map", "smc_events", "sqzmom"]
print("thinking 中出现的工具名:")
hits = {}
for t in TOOLS:
    c = len(re.findall(re.escape(t), full))
    if c:
        hits[t] = c
for t, c in sorted(hits.items(), key=lambda x: -x[1]):
    print(f"  {t:<16} {c}")

# 最终产出
holds = sorted(STATE.glob("*.hold.json"), key=lambda p: p.stat().st_mtime)
if holds:
    h = json.loads(holds[-1].read_text(encoding="utf-8"))
    print(f"\n=== Plan: {h.get('cycle_id')} ===")
    print("reasoning:", (h.get("reasoning") or "")[:600])
    for c in h.get("chips") or []:
        print(f"  chip: {c.get('action')} {c.get('symbol')} conf={c.get('confidence')}")

# 保存
out = Path("verify_data/full-llm-analysis.md")
out.write_text(
    f"# 全方位 LLM 分析（真实行情）\n\n"
    f"- cycle: {d.get('cycle_id')}\n- thinking 轮数: {len(chain)}\n- 字数: {len(full)}\n\n"
    f"## 工具使用\n\n" + "\n".join(f"- `{t}`: {c} 次" for t, c in sorted(hits.items(), key=lambda x: -x[1]))
    + "\n\n## thinking 全文\n\n" + "\n\n".join(f"### round {i}\n\n{c}" for i, c in enumerate(chain, 1)),
    encoding="utf-8",
)
print(f"\n保存: {out}")
