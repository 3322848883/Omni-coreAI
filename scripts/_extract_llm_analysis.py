"""提取 LLM 产出的 ETH 分析（hold.json reasoning 字段）。"""
import json
from pathlib import Path

state = Path("data/bots/skill-e2e/state")
holds = sorted(state.glob("*.hold.json"), key=lambda p: p.stat().st_mtime)
doc = json.loads(holds[-1].read_text(encoding="utf-8"))
reasoning = doc.get("reasoning") or ""
chips = doc.get("chips") or []

print("cycle_id:", doc.get("cycle_id"))
print("chip:", json.dumps(chips[0], ensure_ascii=False) if chips else None)
print("=" * 60)
print(reasoning)

out = Path("verify_data/LLM-eth-analysis-raw.md")
out.write_text(
    f"# LLM（DeepSeek）产出的 ETH 价格行为分析\n\n"
    f"> 生成方式：`gate_bot plan --bot skill-e2e` · skill=price-action-trading · 模型=global:deepseek-v4.1-flash\n"
    f"> cycle_id: {doc.get('cycle_id')}\n\n"
    f"## Plan chips\n\n```json\n{json.dumps(chips, ensure_ascii=False, indent=2)}\n```\n\n"
    f"## reasoning 全文（LLM 原文）\n\n{reasoning}\n",
    encoding="utf-8",
)
print("\n--- saved to", out, "---")
