#!/usr/bin/env python3
"""诊断：读失败轮的 error.json + 用同一个 sizing 入口复算 size_usd，看在哪一步变成 0/None。"""
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

ERR = ROOT / "data/bots/ladder-v5-paper/archive/failed/20261027-131152-eth-ladder-2026-05-21T11.json.error.json"
PLAN = ROOT / "data/bots/ladder-v5-paper/archive/failed/20261027-131152-eth-ladder-2026-05-21T11.json"

print("=== error.json ===")
try:
    d = json.load(open(ERR, encoding="utf-8"))
    print("keys:", list(d.keys()))
    for k in ("error", "message", "traceback", "stack"):
        if k in d:
            print(f"{k}:", str(d[k])[:900])
    print(json.dumps(d, ensure_ascii=False)[:900])
except Exception as e:  # noqa: BLE001
    print("read error:", e)

print("\n=== sizing 复算 ===")
from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/ladder-v5-paper.yaml")
client = bot.create_client()
contracts = {}
try:
    contracts = {c: client.get_contract(c) for c in (bot.symbols or ["ETH_USDT"])}
except Exception as e:  # noqa: BLE001
    print("get_contract error:", e)
meta = None
for sym, c in contracts.items():
    fields = ("quanto_multiplier", "size_min", "size_step", "enable_decimal", "mark_price",
              "order_size_min")
    print(f"contract {sym}:", {k: getattr(c, k, "<none>") for k in fields})
    meta = c

from omnialpha.executor import Executor  # noqa: E402

ex = Executor(client, bot, None) if True else None
plan = json.load(open(PLAN, encoding="utf-8"))
for o in plan.get("orders", []):
    if o.get("action", "").startswith("open"):
        try:
            size = ex._resolve_order_size(o, meta, o.get("price"))
            print(f"  size_usd={o.get('size_usd')} price={o.get('price')} -> size={size!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  size_usd={o.get('size_usd')} price={o.get('price')} -> EXC {type(e).__name__}: {e}")
