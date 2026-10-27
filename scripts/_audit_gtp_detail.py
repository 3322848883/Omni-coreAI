#!/usr/bin/env python3
"""逐条确认 6 条 t-mtg-gtp 限价单是否 reduce_only（防裸空判定）。"""
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()
IDS = ["63894859558222233", "63894859635509067", "63894859486627211",
       "63894859500055035", "63894859593203689", "63894859471594340"]
for oid in IDS:
    o = c.get_order(oid)
    print(json.dumps({"id": oid, "price": o.get("price"), "size": o.get("size"),
                      "left": o.get("left"), "reduce_only": o.get("reduce_only"),
                      "is_reduce_only": o.get("is_reduce_only"),
                      "text": o.get("text"), "status": o.get("status"),
                      "create": o.get("create_time")}, ensure_ascii=False))
