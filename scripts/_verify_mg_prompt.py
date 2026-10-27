#!/usr/bin/env python3
"""端到端验证：按 martin-grid 的真实配置构建快照 + user prompt，确认 boll 三件套进了模型输入。"""
import json
import re
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.strategist import build_user_prompt  # noqa: E402
from omnialpha.strategist.market import MarketConfig  # noqa: E402
from omnialpha.strategist.snapshot import collect_snapshot  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
mk = (bot.strategist or {}).get("market") or {}
print("indicators =", mk.get("indicators"))

# 用 .env 里的只读凭据构建 client（与进程同源）
import os  # noqa: E402

client = GateClient(api_key=os.environ.get("GATE_API_KEY", ""),
                    api_secret=os.environ.get("GATE_API_SECRET", ""),
                    env=bot.env or "live")

raw = mk
mk = MarketConfig(
    mode=raw.get("mode") or "rest_only",
    pa_data_root=raw.get("pa_data_root"),
    indicators=raw.get("indicators") or [],
    extra_timeframes=list(raw.get("timeframes") or []),
    extra_candles=int(raw.get("extra_candles") or 20),
)
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
user = build_user_prompt(snap, "（诊断：只看快照）", bot.symbols)
n_boll = user.count("boll20")
print(f"user prompt 长度 {len(user)} 字符 | boll20 出现 {n_boll} 次")
m = re.search(r'"boll20":\s*([0-9.]+)', user)
m2 = re.search(r'"boll_upper":\s*([0-9.]+)', user)
m3 = re.search(r'"boll_lower":\s*([0-9.]+)', user)
print("样例 boll20 =", m.group(1) if m else None,
      "| boll_upper =", m2.group(1) if m2 else None,
      "| boll_lower =", m3.group(1) if m3 else None)
print("candles 行样例:", re.search(r"\{[^{}]*boll20[^{}]*\}", user).group(0)[:200]
      if re.search(r"\{[^{}]*boll20[^{}]*\}", user) else "无")
print("VERIFY", "OK" if n_boll > 0 else "FAIL")
