#!/usr/bin/env python3
"""BB 判据 A/B 测试：同一快照，基线人格 vs 基线+BB 判据，各跑 2 轮 LLM。

不碰运行中的 bot（独立进程，只读 .env 与 prompts），结果落 /tmp/ab_bb/。
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.strategist import build_system_prompt, build_user_prompt  # noqa: E402
from omnialpha.providers import resolve_llm_config  # noqa: E402
from omnialpha.strategist.llm_client import LLMClient  # noqa: E402
from omnialpha.strategist.market import MarketConfig  # noqa: E402
from omnialpha.strategist.snapshot import collect_snapshot  # noqa: E402

BB_SECTION = """

## 十二、布林带判据（本轮 A/B 测试）

快照每根 K 线自带 `boll20 / boll_upper / boll_lower`（15m 主周期与各 HTF 同源，SMA20±2σ）。
带宽 = (boll_upper − boll_lower) / boll20。ETH 15m 近 21 天实测分布：P50≈1.1%、P25≈0.7%、P90≈2.9%。

定位：波动率 / 均值回归工具，与 SMC 结构位互为确认，**不单独构成入场理由**。

1. 补格闸门（只约束马丁 / 低吸，不影响减仓与 TP）：
   - 现价 ≤ boll_lower 且带宽 < 2.9%（未趋势扩张）→ 低吸档正常解锁；
   - 现价 ≤ boll_lower 且带宽 ≥ 2.9%（趋势扩张，参考 10-08 单日连续穿档）→ 下跌未止，
     本档延后，等出现「收在 boll_lower 之上的反转 K」再补；
   - 现价 ≥ boll_upper → 禁止追多补格；已有持仓的 TP 可上移一档。
2. 压缩预警：带宽 < 0.7%（P25 以下）→ 区间极度压缩，突破会连续穿档，
   马丁最多再补 1 档，并预留反向空间；
3. 回归锚：价格从 boll_lower 下方收回 boll20 之上 → 本轮低吸格可提前 TP 或收紧 SL；
4. 与 SMC 冲突仲裁：SMC 判 premium / 顺势，但现价 < boll_lower（插针）→
   按「极端回归」处理，不顺插针追单，等回中轨再评估。
"""

OUT = Path("/tmp/ab_bb")
OUT.mkdir(exist_ok=True)

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
raw = (bot.strategist or {}).get("market") or {}
mk = MarketConfig(
    mode=raw.get("mode") or "rest_only",
    indicators=raw.get("indicators") or [],
    extra_timeframes=list(raw.get("timeframes") or []),
    extra_candles=int(raw.get("extra_candles") or 20),
)
client = bot.create_client()   # 与进程同源的 client（账户/行情头都正确）

print("building snapshot …")
snap = collect_snapshot(
    client,
    bot.symbols,
    candles=(bot.strategist or {}).get("candles") or 200,
    interval=(bot.strategist or {}).get("timeframe") or "15m",
    market_cfg=mk,
    env=bot.env,
    bot_root=ROOT,
    vision_timeframes=[],
)
user = build_user_prompt(snap, "（A/B 诊断轮：只看快照给计划）", bot.symbols)
base = (ROOT / "prompts/ladder_nogate.md").read_text(encoding="utf-8")
tools_guide = ""  # 与 bot 一致需要 TOOL_GUIDE；这里关工具、纯 prompt 对比更干净

variants = {
    "baseline": build_system_prompt(base, tools_guide=tools_guide, skill_catalog=[]),
    "bb": build_system_prompt(base + BB_SECTION, tools_guide=tools_guide, skill_catalog=[]),
}

llm = LLMClient(resolve_llm_config(ROOT, bot_id="martin-grid",
                                   llm=(bot.strategist or {}).get("llm")))
for name, system in variants.items():
    for i in (1, 2):
        t0 = time.time()
        print(f"--- {name} run{i} …", flush=True)
        try:
            out = llm.chat(system, user)
            (OUT / f"{name}_{i}.txt").write_text(out, encoding="utf-8")
            n_boll = out.count("boll")
            import re
            m = re.search(r'"action":\s*"(\w+)"', out)
            acts = re.findall(r'"action":\s*"(\w+)"', out)
            print(f"    {time.time()-t0:.0f}s  len={len(out)}  boll提及={n_boll}  "
                  f"actions={acts[:8]}")
        except Exception as e:  # noqa: BLE001
            print(f"    FAIL {type(e).__name__}: {e}")
print("\noutputs →", OUT)
