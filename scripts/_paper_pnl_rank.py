# -*- coding: utf-8 -*-
"""模拟盘收益排行：读各 bot paper/account.db 权益 vs 初始 10000。"""
import sqlite3
from pathlib import Path

root = Path("data/bots")
rows = []
for db in sorted(root.glob("*/paper/account.db")):
    bid = db.parents[1].name
    total = avail = upnl = 0.0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        try:
            cur.execute("SELECT * FROM account LIMIT 1")
            cols = [d[0] for d in cur.description]
            acc = cur.fetchone()
            ad = dict(zip(cols, acc)) if acc else {}
        except Exception:
            ad = {}
        try:
            cur.execute("SELECT * FROM positions")
            pcols = [d[0] for d in cur.description]
            positions = [dict(zip(pcols, p)) for p in cur.fetchall()]
        except Exception:
            positions = []
        con.close()
        total = float(ad.get("total") or ad.get("balance") or ad.get("equity") or 0)
        avail = float(ad.get("available") or 0)
        for p in positions:
            try:
                upnl += float(p.get("unrealised_pnl") or p.get("unrealised") or 0)
            except Exception:
                pass
    except Exception as e:
        print(bid, "ERR", e)
        continue
    rows.append((bid, total, avail, upnl))

rows.sort(key=lambda x: x[1], reverse=True)
print(f"{'bot':22} {'equity':>10} {'PnL':>10} {'PnL%':>8} {'uPnL':>8}")
for bid, total, avail, upnl in rows:
    pnl = total - 10000 if total else 0
    pct = pnl / 100 if total else 0
    print(f"{bid:22} {total:10.2f} {pnl:+10.2f} {pct:+7.2f}% {upnl:+8.2f}")
