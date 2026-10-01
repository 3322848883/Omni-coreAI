"""重建上一轮 LLM 请求的数据载荷（发了什么 + 用了什么工具）。"""
import json
import re
from pathlib import Path

ROOT = Path(".")

# 1) thinking.json 的 reasoning_chain 提到的工具
state = ROOT / "data" / "bots" / "skill-e2e" / "state"
thinkings = sorted(state.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime)
doc = json.loads(thinkings[-1].read_text(encoding="utf-8"))
chain = doc.get("reasoning_chain") or []
print("=== thinking 轮数 ===", len(chain))
for i, step in enumerate(chain, 1):
    print(f"\n--- round {i} (len={len(step)}) ---")
    # 找工具意图
    for kw in ("skill", "klines", "indicators", "ticker", "orderbook", "contract",
               "stats", "account", "smc_map", "smc_events", "sqzmom",
               "trades_flow", "liquidations", "market_stats", "tech_analysis",
               "coin_info", "onchain", "social", "overview", "sentiment", "macro"):
        if kw in step:
            print(f"  提到工具: {kw}")
    print("  摘要:", step[:300].replace("\n", " "))

# 2) skill journal
print("\n=== skill journal（最后 3）===")
for line in (ROOT / "logs" / "skill_journal.jsonl").read_text(encoding="utf-8").splitlines()[-3:]:
    print(" ", line)

# 3) 快照结构（决定 text 里带了什么）
from gate_bot.strategist.snapshot import collect_snapshot  # noqa: E402
print("\n=== snapshot 顶层字段（本轮实际会发送的结构）===")
print("→ build_user_prompt 发送内容 = 品种宇宙 + 策略风控 + 【市场与账户快照】JSON + 指令")
print("→ snapshot 含 market.<symbol> = candles(60) + indicators + tf 子周期 + account + ...")
print("→ vision 默认 True → _generate_charts 生成 K 线图 base64 追加 image_url")
