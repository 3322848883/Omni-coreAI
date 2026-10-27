#!/usr/bin/env python3
"""0 张挂单的完整字段：谁挂的、何时、什么状态。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))
from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()
zero = [o for o in c.list_orders("ETH_USDT") if float(o.get("size") or 0) == 0]
print(f"size=0 挂单 {len(zero)} 条：")
for o in zero:
    d = c.get_order(o["id"])
    print(json.dumps({k: d.get(k) for k in
                      ("id", "price", "size", "left", "status", "text",
                       "create_time", "finish_as", "is_reduce_only", "fill_price")},
                     ensure_ascii=False))
