"""只读 AI 分析：用 smc_map / smc_events / sqzmom 测一次实盘效果。

- 拉 Gate 实盘数据（经工具层，和 bot 调用同路径）
- 调 LLM 出分析
- 不写 inbox、不产生 Plan、不下单
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.exchanges.gate import GateExchange
from omnialpha.providers import resolve_llm_config
from omnialpha.strategist.llm_client import LLMClient
from omnialpha.strategist.market import MarketConfig
from omnialpha.strategist.tools import TOOL_GUIDE, run_tool


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("缺 OPENAI_API_KEY（LLM 需要）")
        return 2

    ex = GateExchange(env="live")
    cfg = MarketConfig(mode="rest_only", exchange="gate", indicators=["ema20", "rsi14", "atr14"])

    print("== 拉取工具数据 ==")
    tools_data = {}
    for name, args in [
        ("ticker", {"symbol": "BTC_USDT"}),
        ("klines", {"symbol": "BTC_USDT", "tf": "15m", "limit": 5}),
        ("indicators", {"symbol": "BTC_USDT", "tf": "15m", "limit": 30,
                        "names": ["ema20", "ema50", "rsi14", "atr14", "macd", "boll20"]}),
        ("smc_map", {"symbol": "BTC_USDT", "tf": "15m", "limit": 200}),
        ("smc_events", {"symbol": "BTC_USDT", "tf": "15m", "limit": 250}),
        ("sqzmom", {"symbol": "BTC_USDT", "tf": "15m", "limit": 150}),
    ]:
        out = run_tool(ex, name, args, env="live", market_cfg=cfg)
        tools_data[name] = out
        err = out.get("error")
        extra = ""
        if name == "smc_map":
            extra = f" swing={out.get('swing_trend')} internal={out.get('internal_trend')}"
        elif name == "smc_events":
            extra = f" trend={out.get('trend')} last={out.get('last_event')}"
        elif name == "sqzmom":
            extra = f" mom={out.get('sqz_mom')} state={out.get('sqz_state')}"
        elif name == "ticker":
            extra = f" last={out.get('last')}"
        print(f"  {name}: {'ERR ' + str(err) if err else 'ok'}{extra}")

    print("\n== 调用 LLM 分析 ==")
    llm_cfg = resolve_llm_config(ROOT, bot_id="smc-readonly-test", llm={"provider": "gw-flash"})
    llm = LLMClient(llm_cfg)

    system = (
        "你是加密货币永续合约交易分析师。基于给出的工具数据做客观分析。\n"
        "严格要求：只输出分析，不输出 Plan JSON、不给出下单指令、不使用 tool_calls。\n"
        "结构：1)行情现状 2)SMC地图(方向/估值区/关键位) 3)结构事件(触发/扫荡) 4)Squeeze动量 "
        "5)多空观点与风险 6)结论一句话。中文，简洁。\n"
    )
    user = (
        "以下是 BTC_USDT 15m 的实盘工具数据（JSON）：\n\n"
        + json.dumps(tools_data, ensure_ascii=False, default=str)
        + "\n\n请输出分析。"
    )

    text = llm.chat(system, user)
    print("\n===== AI 分析结果 =====\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
