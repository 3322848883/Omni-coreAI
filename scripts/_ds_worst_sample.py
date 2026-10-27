#!/usr/bin/env python3
"""打印 ohlc_sample 门禁的最差样本详情。"""
import json
import subprocess

out = subprocess.run(
    ["python3", "_migrate_v2_patched.py", "--db", "data/kline.db", "--dry-run"],
    cwd="/opt/omnialpha/pa-data-source", capture_output=True, text=True, timeout=280)
d = json.loads(out.stdout)
g = d["gates"]["ohlc_sample"]
print("worst_rel =", g.get("worst_rel"), "| samples =", g.get("samples"))
print("worst_detail =", json.dumps(g.get("worst_detail"), ensure_ascii=False, indent=1))
