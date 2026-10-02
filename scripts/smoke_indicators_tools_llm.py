"""全量可用性验收：指标 / 工具 / LLM 一次跑通。"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.exchanges.registry import create_exchange
from omnialpha.providers import resolve_llm_config
from omnialpha.strategist.indicators import attach_indicators, parse_indicator_name
from omnialpha.strategist.llm_client import LLMClient
from omnialpha.strategist.market import MarketConfig
from omnialpha.strategist.tools import NATIVE_TOOLS, TOOL_NAMES, run_tool

CFG = MarketConfig(mode="rest_only", exchange="gate", indicators=["ema20", "rsi14", "atr14"])

# 23 族代表名（含任意周期样本）
INDICATOR_SAMPLES = [
    "ema20", "ema50", "sma20", "rma14", "wma14", "vwma20",
    "atr14", "atr14_ema", "atr14_sma",
    "rsi14", "rsi14_ema", "rsi14_bb",
    "macd", "macd_dea", "macd_hist", "macd_sma",
    "boll20", "boll20_ema", "boll_upper", "boll_lower",
    "stoch14", "stoch_d14", "cci20", "wr14", "mfi14",
    "adx14", "plus_di", "vwap", "obv", "supertrend10",
    "pine_ema", "ema_smooth",
    "sqzmom", "sqzmom_state",
]

TOOL_ARGS = {
    "klines": {"symbol": "BTC_USDT", "tf": "15m", "limit": 5},
    "indicators": {"symbol": "BTC_USDT", "tf": "15m", "limit": 30,
                   "names": ["ema20", "rsi14", "atr14", "macd"]},
    "ticker": {"symbol": "BTC_USDT"},
    "orderbook": {"symbol": "BTC_USDT", "limit": 5},
    "contract": {"symbol": "BTC_USDT"},
    "stats": {"symbol": "BTC_USDT"},
    "account": {},
    "smc_map": {"symbol": "BTC_USDT", "tf": "15m", "limit": 150},
    "smc_events": {"symbol": "BTC_USDT", "tf": "15m", "limit": 200},
    "sqzmom": {"symbol": "BTC_USDT", "tf": "15m", "limit": 120},
    "trades_flow": {"symbol": "BTC_USDT", "limit": 5},
    "liquidations": {"symbol": "BTC_USDT", "limit": 5},
    "market_stats": {"symbol": "BTC_USDT", "limit": 5},
    "tech_analysis": {"symbol": "BTC_USDT"},
    "coin_info": {"symbol": "BTC_USDT"},
    "onchain": {"token": "BTC"},
    "social": {"coin": "BTC", "limit": 5},
    "overview": {"limit": 5},
    "sentiment": {"coin": "BTC", "limit": 5},
    "macro": {"limit": 5},
}


def main() -> int:
    results = {"indicators": {}, "tools": {}, "llm": {}}

    # 账户类工具需要密钥；从环境加载（不落盘）
    import os as _os
    _key = _os.environ.get("GATE_API_KEY") or _os.environ.get("GATE_TESTNET_API_KEY") or ""
    _sec = _os.environ.get("GATE_API_SECRET") or _os.environ.get("GATE_TESTNET_API_SECRET") or ""

    print("=" * 60)
    print("1) 指标族实测（attach_indicators 产出非 None）")
    print("=" * 60)
    ex = create_exchange("gate", env="live", api_key=_key, api_secret=_sec)
    rows = ex.get_klines("BTC_USDT", "15m", 250)
    print(f"   行情 rows={len(rows)}")
    attach_indicators(rows, INDICATOR_SAMPLES)
    last = rows[-1]
    for name in INDICATOR_SAMPLES:
        v = last.get(name)
        ok = v is not None
        results["indicators"][name] = ok
        mark = "OK" if ok else "NULL"
        print(f"   [{mark}] {name:<14} {v}")
    # 族计数
    kinds = set()
    for n in INDICATOR_SAMPLES:
        try:
            kinds.add(parse_indicator_name(n)["kind"])
        except Exception:
            pass
    ind_ok = sum(1 for v in results["indicators"].values() if v)
    print(f"\n   小计: {ind_ok}/{len(INDICATOR_SAMPLES)} 有值, {len(kinds)} 族")

    print("\n" + "=" * 60)
    print("2) 20 个工具实测")
    print("=" * 60)
    print(f"   TOOL_NAMES={len(TOOL_NAMES)} NATIVE_TOOLS={len(NATIVE_TOOLS)}")
    for name in TOOL_NAMES:
        args = TOOL_ARGS.get(name, {})
        try:
            out = run_tool(ex, name, args, env="live", market_cfg=CFG)
            err = out.get("error") if isinstance(out, dict) else None
            if err:
                results["tools"][name] = f"ERR: {err}"
                print(f"   [ERR] {name:<14} {err}")
            else:
                results["tools"][name] = "ok"
                keys = list(out.keys())[:5] if isinstance(out, dict) else []
                print(f"   [OK ] {name:<14} {keys}")
        except Exception as e:  # noqa: BLE001
            results["tools"][name] = f"EXC: {type(e).__name__}"
            print(f"   [EXC] {name:<14} {type(e).__name__}: {e}")
    tools_ok = sum(1 for v in results["tools"].values() if v == "ok")
    print(f"\n   小计: {tools_ok}/{len(TOOL_NAMES)} ok")

    print("\n" + "=" * 60)
    print("3) LLM 连通（gw-flash）")
    print("=" * 60)
    if not os.environ.get("OPENAI_API_KEY"):
        results["llm"] = {"status": "NO KEY"}
        print("   [ERR] 缺 OPENAI_API_KEY")
    else:
        try:
            cfg = resolve_llm_config(ROOT, bot_id="smoke", llm={"provider": "gw-flash"})
            llm = LLMClient(cfg)
            text = llm.chat("你是测试助手。", "只回复两个字：可用")
            results["llm"] = {"status": "ok", "reply": (text or "")[:80]}
            print(f"   [OK ] reply={text!r}")
        except Exception as e:  # noqa: BLE001
            results["llm"] = {"status": f"ERR {type(e).__name__}: {e}"[:120]}
            print(f"   [ERR] {type(e).__name__}: {e}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    print(f"  指标 {ind_ok}/{len(INDICATOR_SAMPLES)}  |  工具 {tools_ok}/{len(TOOL_NAMES)}  |  LLM {results['llm'].get('status')}")
    out_path = ROOT / "logs" / "smoke_indicators_tools_llm.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  详情 → {out_path}")
    return 0 if (ind_ok >= len(INDICATOR_SAMPLES) and tools_ok >= 18 and results["llm"].get("status") == "ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
