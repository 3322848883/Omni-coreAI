"""行为级 A/B：catalog hint 默认语义 vs 必载语义，真实 LLM 下 skill 调用率。

本地跑（不打交易所）：只挂 skill + skill_ref 工具 schema，模型返回 tool_calls 即记录。

v2：多场景设计，破解天花板效应——
  S1 clear    清晰开仓信号（v2 原快照，default 也大概率调）
  S2 ambiguous 结构模糊/区间中部，模型可能判断「无需技能」
  S3 noisy    长 [近况]+多工具数据干扰，注意力被稀释

用法：python scripts/_ab_skill_required.py [--rounds 10] [--scenarios S1,S2,S3]
"""
import argparse
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from omnialpha.skillkit import SkillRegistry, render_catalog
from omnialpha.strategist.llm_client import LLMClient
from omnialpha.strategist.loop import TOOL_GUIDE, build_system_prompt
from omnialpha.providers import resolve_llm_config
from omnialpha.skillkit.tool import SKILL_TOOL_DEF, SKILL_REF_TOOL_DEF

S1_CLEAR = """[本轮快照]
symbol: ETH_USDT  tf: 15m  现价: 2556.3
15m K线(最近6根): 2552.8→2554.1→2551.6→2555.9→2553.2→2556.3，EMA20=2552.1(上行)，EMA50=2548.7
ATR14(15m)=6.8；RSI14=58
结构：4h 多头背景；15m 突破 2554 后回踩 2552 企稳，微 HL 链 2551.6→2553.2；
tape: taker delta 近 3 根 +12k/+8k/+15k，CVD 回升
账户: flat 无仓无挂单；权益 72.4U 可用 72.4U
任务: 按输出契约输出本轮 Plan JSON。"""

S2_AMBIGUOUS = """[本轮快照]
symbol: ETH_USDT  tf: 15m  现价: 2550.9
15m K线(最近6根): 2553.4→2548.2→2551.7→2549.5→2552.3→2550.9，EMA20=2551.0(走平)，EMA50=2549.9
ATR14(15m)=7.4；RSI14=49
结构：4h 方向不明；15m 在 2546-2556 区间内震荡，上一根十字星，HL/LH 交替无连续性；
tape: taker delta 近 3 根 -3k/+5k/-2k，CVD 横盘；盘口买卖深度基本对称
账户: flat 无仓无挂单；权益 72.4U 可用 72.4U
任务: 按输出契约输出本轮 Plan JSON。若不交易，输出 hold 决策。"""

S3_NOISY = """[近况]
- 3轮前 cycle=eth-1711 decision=hold reasoning=区间上沿遇阻，等待回踩确认……(截断)
- 2轮前 cycle=eth-1712 decision=hold reasoning=EMA20 走平，CVD 无方向，观望……(截断)
- 1轮前 cycle=eth-1713 decision=hold reasoning=盘口多空僵持，无微结构入场点……(截断)

[上轮方案状态]
上轮为 hold，无挂单无需管理。journal_lookup(eth-1713) 可查完整记录。

[本轮快照]
symbol: ETH_USDT  tf: 15m  现价: 2554.7
15m K线(最近6根): 2550.2→2551.8→2549.6→2552.4→2553.9→2554.7，EMA20=2551.4(微上行)，EMA50=2549.2
ATR14(15m)=6.9；RSI14=55
结构：4h 多头偏多但 1h 中性；15m 缓慢爬升，未突破 2556 前高，也未回踩 EMA20；
tape: taker delta +4k/+6k/+3k 温和；orderbook: bid 2548-2550 略厚
账户: flat 无仓无挂单；权益 72.4U 可用 72.4U
任务: 按输出契约输出本轮 Plan JSON。"""

SCENARIOS = {
    "S1_clear": S1_CLEAR,
    "S2_ambiguous": S2_AMBIGUOUS,
    "S3_noisy": S3_NOISY,
}


def run(llm, system, tools, snapshot, rounds, tag):
    hits = 0
    done = 0
    for i in range(rounds):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": snapshot}]
        try:
            msg = llm.chat_message_full(msgs, tools=tools, tool_choice="auto")
        except Exception as e:
            print(f"  [{tag} r{i+1}] LLM ERR {str(e)[:90]}")
            continue
        calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        names = []
        for c in calls or []:
            fn = c.get("function") or {}
            names.append(fn.get("name"))
        skill_hit = "skill" in (names or [])
        hits += 1 if skill_hit else 0
        done += 1
        print(f"  [{tag} r{i+1}] tool_calls={names} skill={'Y' if skill_hit else 'N'}")
        sys.stdout.flush()
    return hits, done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--scenarios", type=str, default="S1_clear,S2_ambiguous,S3_noisy")
    a = ap.parse_args()

    reg = SkillRegistry()
    reg.scan([REPO / "skills"])
    metas = reg.visible_for("brooks-eth", ["price-action-trading"])
    assert metas, "skills/price-action-trading 缺失（gitignored 个人内容）"

    slim = (REPO / "prompts" / "brooks_eth_pa_slim.md").read_text(encoding="utf-8")
    llm_cfg = resolve_llm_config(REPO, "brooks-eth", {"provider": "gw-cn-flash"})
    llm = LLMClient(llm_cfg)
    tools = [SKILL_TOOL_DEF, SKILL_REF_TOOL_DEF]

    scen_names = [s.strip() for s in a.scenarios.split(",") if s.strip() in SCENARIOS]
    # 汇总表 {scenario: {mode: (hits, done)}}
    results = {s: {} for s in scen_names}

    # 先跑 default 全部场景，再跑 required 全部场景（模式内随机穿插无意义，单线程顺序即可）
    for mode, required in (("default", False), ("required", True)):
        cat = render_catalog(metas, required=required)
        system = build_system_prompt(slim, tools_guide=TOOL_GUIDE, skill_catalog=cat)
        for s in scen_names:
            print(f"\n===== {mode} / {s}（{a.rounds} 轮）=====")
            h, d = run(llm, system, tools, SCENARIOS[s], a.rounds, f"{mode[:4]}-{s.split('_')[0]}")
            results[s][mode] = (h, d)

    print("\n===== 汇总（skill 调用 / 有效轮）=====")
    for s in scen_names:
        d_h, d_d = results[s].get("default", (0, 0))
        r_h, r_d = results[s].get("required", (0, 0))
        print(f"{s:14s}  default {d_h}/{d_d}   required {r_h}/{r_d}")
    tot_d = [results[s].get("default", (0, 0)) for s in scen_names]
    tot_r = [results[s].get("required", (0, 0)) for s in scen_names]
    print(f"{'TOTAL':14s}  default {sum(x[0] for x in tot_d)}/{sum(x[1] for x in tot_d)}"
          f"   required {sum(x[0] for x in tot_r)}/{sum(x[1] for x in tot_r)}")


if __name__ == "__main__":
    main()
