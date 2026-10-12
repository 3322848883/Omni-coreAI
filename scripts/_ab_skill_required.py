"""行为级 A/B：catalog hint 默认语义 vs 必载语义，真实 LLM 下 skill 调用率。

本地跑（不打交易所）：只挂 skill + klines 工具 schema，模型返回 tool_calls 即记录。
用法：python scripts/_ab_skill_required.py [--rounds 5]
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from omnialpha.skillkit import SkillRegistry, render_catalog
from omnialpha.strategist.llm_client import LLMClient
from omnialpha.strategist.loop import TOOL_GUIDE, build_system_prompt
from omnialpha.providers import resolve_llm_config
from omnialpha.skillkit.tool import SKILL_TOOL_DEF, SKILL_REF_TOOL_DEF

SNAPSHOT = """[本轮快照]
symbol: ETH_USDT  tf: 15m  现价: 2556.3
15m K线(最近6根): 2552.8→2554.1→2551.6→2555.9→2553.2→2556.3，EMA20=2552.1(上行)，EMA50=2548.7
ATR14(15m)=6.8；RSI14=58
结构：4h 多头背景；15m 突破 2554 后回踩 2552 企稳，微 HL 链 2551.6→2553.2；
tape: taker delta 近 3 根 +12k/+8k/+15k，CVD 回升
账户: flat 无仓无挂单；权益 72.4U 可用 72.4U
任务: 按输出契约输出本轮 Plan JSON。"""


def run(llm, system, tools, rounds):
    hits = 0
    for i in range(rounds):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": SNAPSHOT}]
        try:
            msg = llm.chat_message_full(msgs, tools=tools, tool_choice="auto")
        except Exception as e:
            print(f"  r{i+1}: LLM ERR {str(e)[:100]}")
            continue
        calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        names = []
        for c in calls or []:
            fn = c.get("function") or {}
            names.append(fn.get("name"))
        skill_hit = "skill" in (names or [])
        hits += 1 if skill_hit else 0
        print(f"  r{i+1}: tool_calls={names} skill={'Y' if skill_hit else 'N'}")
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    a = ap.parse_args()

    reg = SkillRegistry()
    reg.scan([REPO / "skills"])
    metas = reg.visible_for("brooks-eth", ["price-action-trading"])
    assert metas, "skills/price-action-trading 缺失（gitignored 个人内容）"

    slim = (REPO / "prompts" / "brooks_eth_pa_slim.md").read_text(encoding="utf-8")
    llm_cfg = resolve_llm_config(REPO, "brooks-eth", {"provider": "gw-cn-flash"})
    llm = LLMClient(llm_cfg)
    tools = [SKILL_TOOL_DEF, SKILL_REF_TOOL_DEF]

    results = {}
    for mode, required in (("default", False), ("required", True)):
        cat = render_catalog(metas, required=required)
        system = build_system_prompt(slim, tools_guide=TOOL_GUIDE, skill_catalog=cat)
        print(f"\n===== {mode}（{a.rounds} 轮）=====")
        results[mode] = run(llm, system, tools, a.rounds)

    print("\n===== 汇总 =====")
    for mode, hits in results.items():
        print(f"{mode}: skill 调用 {hits}/{a.rounds}")


if __name__ == "__main__":
    main()
