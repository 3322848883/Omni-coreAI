#!/usr/bin/env python3
"""从服务器导出 ETH 15m 指标样本（最近 400 根 + 20 天内高波动一根）。"""
import sqlite3
import json

DB = "/opt/omnialpha/pa-data-source/data/kline.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT t, o, h, l, c, atr14, boll20, boll20_upper, boll20_lower FROM kline "
    "WHERE symbol='ETH_USDT' AND interval='15m' AND closed=1 "
    "AND atr14 IS NOT NULL AND boll20 IS NOT NULL ORDER BY t DESC LIMIT 400").fetchall()
# 挑一根历史高波动 bar（带宽最大）
hi = con.execute(
    "SELECT t, o, h, l, c, atr14, boll20, boll20_upper, boll20_lower, "
    "((boll20_upper-boll20_lower)/boll20) AS bw FROM kline "
    "WHERE symbol='ETH_USDT' AND interval='15m' AND closed=1 "
    "AND atr14 IS NOT NULL AND boll20>0 ORDER BY bw DESC LIMIT 1").fetchone()
con.close()
out = {"recent": [list(r) for r in rows], "high_vol": list(hi[:9]) if hi else None}
print(json.dumps(out))
