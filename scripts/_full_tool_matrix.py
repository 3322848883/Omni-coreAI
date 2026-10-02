"""真实行情全方位测试：全部 22 个工具 + skill 全链路（L1/L2/L3）。

用 Gate testnet 真实行情，逐个调用工具，统计成功/失败/耗时。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.strategist.tools import NATIVE_TOOLS, TOOL_NAMES, run_tool  # noqa: E402

SYM = "BTC_USDT"
TF = "1h"

# 每个工具的调用参数（只读，不下单）
CASES = [
    ("klines", {"symbol": SYM, "tf": TF, "limit": 50}),
    ("indicators", {"symbol": SYM, "tf": TF, "names": ["ema20", "rsi14", "atr14", "macd"], "limit": 30}),
    ("ticker", {"symbol": SYM}),
    ("orderbook", {"symbol": SYM, "limit": 5}),
    ("contract", {"symbol": SYM}),
    ("stats", {"symbol": SYM}),
    ("account", {}),
    ("trades_flow", {"symbol": SYM, "tf": TF, "limit": 30}),
    ("liquidations", {"symbol": SYM, "limit": 30}),
    ("market_stats", {"symbol": SYM}),
    ("tech_analysis", {"symbol": SYM, "tf": TF}),
    ("coin_info", {"symbol": SYM}),
    ("onchain", {"symbol": SYM}),
    ("social", {"symbol": SYM}),
    ("overview", {"symbol": SYM}),
    ("sentiment", {"symbol": SYM}),
    ("macro", {"symbol": SYM}),
    ("smc_map", {"symbol": SYM, "tf": TF, "limit": 100}),
    ("smc_events", {"symbol": SYM, "tf": TF, "limit": 100}),
    ("sqzmom", {"symbol": SYM, "tf": TF, "limit": 100}),
    ("skill", {"name": "price-action-trading"}),
    ("skill_ref", {"name": "price-action-trading", "path": "references/SOUL.md"}),
]


def summarize(res):
    if not isinstance(res, dict):
        return f"type={type(res).__name__}"
    if "error" in res:
        return f"ERROR: {str(res['error'])[:80]}"
    keys = list(res.keys())[:6]
    return f"keys={keys}"


def main() -> int:
    client = GateClient(
        api_key=os.environ.get("GATE_TESTNET_API_KEY", ""),
        api_secret=os.environ.get("GATE_TESTNET_API_SECRET", ""),
        env="testnet",
    )
    print("=" * 78)
    print(f"真实行情全方位测试 · {SYM} {TF} · testnet · {len(TOOL_NAMES)} 个工具")
    print("=" * 78)
    print(f"{'工具':<16}{'耗时':>8}  {'结果':<12} 摘要")
    print("-" * 78)

    ok, fail = 0, 0
    rows = []
    for name, args in CASES:
        t0 = time.time()
        try:
            res = run_tool(client, name, args, env="testnet", bot_root=str(ROOT),
                           bot_id="full-test", skill_ids=["price-action-trading"])
            dt = time.time() - t0
            is_err = isinstance(res, dict) and "error" in res
            status = "ERROR" if is_err else "OK"
            if is_err:
                fail += 1
            else:
                ok += 1
            line = summarize(res)
        except Exception as e:  # noqa: BLE001
            dt = time.time() - t0
            status = "EXC"
            fail += 1
            line = f"{type(e).__name__}: {str(e)[:70]}"
        rows.append((name, dt, status, line))
        print(f"{name:<16}{dt:>7.2f}s  {status:<12} {line}")

    print("-" * 78)
    print(f"合计: {ok} OK / {fail} 失败 / {len(CASES)} 总数")

    # 保存报告
    out = ROOT / "verify_data" / "full-tool-matrix.md"
    lines = [
        f"# 真实行情全工具矩阵 · {SYM} {TF}",
        "",
        f"> {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} · Gate testnet · 工具数 {len(TOOL_NAMES)}",
        "",
        "| 工具 | 耗时 | 结果 | 摘要 |",
        "|------|-----:|:---:|------|",
    ]
    for name, dt, status, line in rows:
        lines.append(f"| `{name}` | {dt:.2f}s | {status} | {line} |")
    lines.append("")
    lines.append(f"**合计：{ok} OK / {fail} 失败 / {len(CASES)} 总数**")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n保存: {out}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
