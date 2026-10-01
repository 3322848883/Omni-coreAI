"""提取 skill-real 的真实行情分析产出。"""
import json
import re
from pathlib import Path

STATE = Path("data/bots/skill-real/state")
print("state 文件:")
for p in sorted(STATE.glob("*"), key=lambda x: x.stat().st_mtime):
    print(" ", p.name, p.stat().st_size)

th = sorted(STATE.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime)
if th:
    d = json.loads(th[-1].read_text(encoding="utf-8"))
    chain = d.get("reasoning_chain") or []
    full = "\n".join(chain)
    print(f"\n=== thinking: {d.get('cycle_id')} rounds={len(chain)} chars={len(full)} ===")
    TOOLS = ["skill", "skill_ref", "klines", "indicators", "ticker", "orderbook", "contract",
             "stats", "account", "trades_flow", "liquidations", "market_stats", "tech_analysis",
             "coin_info", "onchain", "social", "overview", "sentiment", "macro",
             "smc_map", "smc_events", "sqzmom"]
    hits = {t: len(re.findall(re.escape(t), full)) for t in TOOLS}
    hits = {k: v for k, v in hits.items() if v}
    print("工具命中:", dict(sorted(hits.items(), key=lambda x: -x[1])))

holds = sorted(STATE.glob("*.hold.json"), key=lambda p: p.stat().st_mtime)
inbox = sorted(Path("data/bots/skill-real/inbox").glob("*.json"), key=lambda p: p.stat().st_mtime)
print("\n=== 最终产物 ===")
if inbox:
    sig = json.loads(inbox[-1].read_text(encoding="utf-8"))
    print("SIGNAL:", json.dumps(sig, ensure_ascii=False, indent=2)[:1500])
elif holds:
    h = json.loads(holds[-1].read_text(encoding="utf-8"))
    print("HOLD:", json.dumps(h, ensure_ascii=False, indent=2)[:1500])

# 保存全文
out = Path("verify_data/real-market-analysis.md")
blocks = [f"# BTC 真实行情价格行为分析（LLM 产出）\n",
          f"> cycle: {d.get('cycle_id') if th else '?'} · 模型 global:deepseek-v4.1-flash · skill: price-action-trading\n",
          f"## 工具使用\n"]
blocks.append("\n".join(f"- `{k}`: {v} 次" for k, v in sorted(hits.items(), key=lambda x: -x[1])))
if inbox:
    blocks.append("\n## 最终信号\n\n```json\n" + json.dumps(json.loads(inbox[-1].read_text(encoding='utf-8')), ensure_ascii=False, indent=2) + "\n```")
elif holds:
    blocks.append("\n## 最终 Plan\n\n```json\n" + json.dumps(json.loads(holds[-1].read_text(encoding='utf-8')), ensure_ascii=False, indent=2) + "\n```")
if th:
    blocks.append("\n## thinking 全文\n\n" + "\n\n".join(f"### round {i}\n\n{c}" for i, c in enumerate(chain, 1)))
out.write_text("\n".join(blocks), encoding="utf-8")
print(f"\n保存: {out}")
