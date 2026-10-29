# -*- coding: utf-8 -*-
"""只读：打印线上 list_orders 原始字段（查 prune 为什么没撤）。"""
import json
import os
import sys

sys.path.insert(0, "/opt/omnialpha")
from dotenv import load_dotenv  # noqa: E402

load_dotenv("/opt/omnialpha/.env")
from omnialpha.gate_client import GateClient  # noqa: E402

c = GateClient(
    api_key=os.getenv("GATE_API_KEY") or os.getenv("GATE_KEY"),
    api_secret=os.getenv("GATE_API_SECRET") or os.getenv("GATE_SECRET"),
)
orders = c.list_orders("ETH_USDT") or []
print("count:", len(orders))
for o in orders[:3]:
    print(json.dumps(o, ensure_ascii=False)[:400])
    print("  size=", repr(o.get("size")), "price=", repr(o.get("price")),
          "left=", repr(o.get("left")),
          "reduce_only=", o.get("is_reduce_only"), o.get("reduce_only"))
