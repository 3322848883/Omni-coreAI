# -*- coding: utf-8 -*-
"""导出 ETH 15m K 线 + 4H EMA200 序列（JSON 到 stdout），供本地回放与画图。"""
import json
import sqlite3

DB = "pa-data-source/data/kline.db"
N = 800

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
bars = []
for t, o, h, l, c, v in con.execute(
        "SELECT t, o, h, l, c, v FROM kline WHERE symbol='ETH_USDT' AND interval='15m' "
        "AND closed=1 ORDER BY t DESC LIMIT ?", (N,)):
    bars.append({"t": int(t), "o": float(o), "h": float(h), "l": float(l),
                 "c": float(c), "v": float(v or 0)})
bars.reverse()

h4 = []
for t, c, e200 in con.execute(
        "SELECT t, c, ema200 FROM kline WHERE symbol='ETH_USDT' AND interval='4h' "
        "AND closed=1 ORDER BY t DESC LIMIT 200"):
    if e200 is not None:
        h4.append({"t": int(t), "c": float(c), "ema200": float(e200)})
h4.reverse()
con.close()

print(json.dumps({"bars": bars, "h4": h4}))
