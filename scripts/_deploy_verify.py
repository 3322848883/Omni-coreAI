#!/usr/bin/env python3
"""部署验证：库行数/最新 t、健康端点、日志尾部。"""
import json
import sqlite3
import subprocess
import time
import urllib.request

DB = "/opt/omnialpha/pa-data-source/data/kline.db"
now = time.time()

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
print(f"{'sym':10s} {'tf':>4s} {'rows':>6s} {'lag_min':>8s}")
for sym, tf, n, mx in con.execute(
        "SELECT symbol, interval, count(*), max(t) FROM kline GROUP BY symbol, interval "
        "ORDER BY symbol, interval"):
    print(f"{sym:10s} {tf:>4s} {n:>6d} {(now - mx)/60:>8.1f}")
con.close()

for port in (18080, 18081):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            d = json.loads(r.read())
            fresh = d.get("data_freshness") or {}
            print(f"health:{port} = {r.status} status={d.get('status')} "
                  f"stale={fresh.get('stale')}/{fresh.get('total')}")
    except Exception as e:  # noqa: BLE001
        print(f"health:{port} = ERR {e}")

for unit in ("omnialpha-kline-live", "omnialpha-kline-testnet"):
    act = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
    print(f"{unit}: {act}")
