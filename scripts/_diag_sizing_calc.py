#!/usr/bin/env python3
"""直接调 Executor._resolve_order_size 复算失败轮的每条腿，定位哪条给出 0 张。"""
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/ladder-v5-paper.yaml")
client = bot.create_client()
ex = Executor(client, symbols_whitelist=list(bot.symbols or []),
              max_notional_usd=bot.max_notional_usd)
meta = client.get_contract("ETH_USDT")

from omnialpha.schema import Intent  # noqa: E402  可能路径不同，失败则回退

plan = json.load(open(ROOT / "data/bots/ladder-v5-paper/archive/failed/"
                      "20261027-131152-eth-ladder-2026-05-21T11.json", encoding="utf-8"))

for o in plan.get("orders", []):
    if not str(o.get("action", "")).startswith("open"):
        continue
    try:
        fields = {f.name for f in Intent.__dataclass_fields__.values()}
        payload = {k: v for k, v in o.items() if k in fields}
        payload.setdefault("action", "open_short")
        payload.setdefault("order_type", o.get("type", "market"))
        intent = Intent(**payload)
    except Exception as e:  # noqa: BLE001
        print("intent build failed:", e)
        break
    try:
        size, notional, basis = ex._resolve_order_size(intent, meta, o.get("price"))
        print(f"size_usd={o.get('size_usd')} price={o.get('price')} -> size={size} "
              f"notional={notional:.2f} basis={basis}")
    except Exception as e:  # noqa: BLE001
        print(f"size_usd={o.get('size_usd')} price={o.get('price')} -> EXC "
              f"{type(e).__name__}: {e}")
