#!/usr/bin/env python3
"""查 3 条触发单的 initial 完整字段（重点：reduce_only / close / size）。"""
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()
for pid in ("2115100973377523712", "2115100963676098560", "2115089030654726144"):
    o = c.get_price_order(pid)
    init = o.get("initial") or {}
    print(json.dumps({"id": pid, "trigger": o.get("trigger"),
                      "initial": init, "status": o.get("status"),
                      "text": o.get("text"), "create": o.get("create_time")},
                     ensure_ascii=False))
