"""真实行情测试三个 TV 工具。"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.strategist.tools import run_tool  # noqa: E402

c = GateClient(api_key=os.environ.get("GATE_TESTNET_API_KEY", ""),
               api_secret=os.environ.get("GATE_TESTNET_API_SECRET", ""),
               env="testnet")

CASES = [
    ("tv_linreg_trendlines", {"symbol": "BTC_USDT", "tf": "1h", "limit": 200, "length": 100}),
    ("tv_rsi_yata", {"symbol": "BTC_USDT", "tf": "1h", "limit": 200, "length": 14}),
    ("tv_lr_ha_candles", {"symbol": "BTC_USDT", "tf": "1h", "limit": 200, "length": 9}),
]

print("=" * 74)
print("TV 指标工具 · 真实行情（Gate testnet）")
print("=" * 74)
for name, args in CASES:
    t0 = time.time()
    r = run_tool(c, name, args, env="testnet", bot_root=str(ROOT))
    dt = time.time() - t0
    err = r.get("error") if isinstance(r, dict) else None
    print(f"\n--- {name} ({dt:.2f}s) ---")
    if err:
        print("  ERROR:", err)
    else:
        print(json.dumps(r, ensure_ascii=False, indent=2)[:1100])
