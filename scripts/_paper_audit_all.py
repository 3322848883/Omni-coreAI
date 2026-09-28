# -*- coding: utf-8 -*-
"""模拟盘全面体检：权益/重复成交/超仓/孤儿单/进程。"""
import sqlite3
from pathlib import Path

INIT = 10000.0
root = Path("data/bots")
rows = []
print("=" * 88)
print(f"{'bot':22} {'equity':>10} {'PnL%':>8} {'dupFill':>8} {'maxNotional':>12} {'pos':>5} {'orphanPO':>8}")
print("=" * 88)

for db in sorted(root.glob("*/paper/account.db")):
    bid = db.parents[1].name
    total = 0.0
    dup = 0
    max_notional = 0.0
    pos_n = 0
    orphan = 0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT * FROM account LIMIT 1")
        cols = [d[0] for d in cur.description]
        acc = cur.fetchone()
        ad = dict(zip(cols, acc)) if acc else {}
        total = float(ad.get("balance") or 0) + float(ad.get("unrealised_pnl") or 0)
        # balance already excludes? equity = balance + unrealised
        bal = float(ad.get("balance") or 0)
        upnl = float(ad.get("unrealised_pnl") or 0)
        total = bal  # PnL rank used balance as total earlier; keep consistent with rank script
        # actually rank used total field if exists
        total = float(ad.get("total") or bal)

        cur.execute("SELECT order_id, COUNT(*) c FROM fills GROUP BY order_id HAVING c>1")
        dup = sum(r[1] - 1 for r in cur.fetchall())

        # positions
        cur.execute("SELECT * FROM positions")
        pcols = [d[0] for d in cur.description]
        positions = [dict(zip(pcols, p)) for p in cur.fetchall() if abs(float(dict(zip(pcols, p)).get("size") or 0)) > 0]
        pos_n = len(positions)
        try:
            cur.execute("SELECT contract, MAX(ABS(size)) FROM fills GROUP BY contract")
            quanto_map = {"BTC_USDT": 0.0001, "ETH_USDT": 0.01}
            for c, s in cur.fetchall():
                q = quanto_map.get(c, 1)
                # approximate notional at ~83k/2700 — better use fill price
            cur.execute("SELECT MAX(ABS(size)*price) FROM fills")
            row = cur.fetchone()
        except Exception:
            pass
        # max fill notional approx size*price (quanto applied later per contract)
        cur.execute("SELECT contract, size, price FROM fills")
        for c, s, p in cur.fetchall():
            q = 0.0001 if c == "BTC_USDT" else 0.01 if c == "ETH_USDT" else 1
            n = abs(float(s)) * q * float(p)
            if n > max_notional:
                max_notional = n

        # orphan price orders: open/untriggered without matching position side
        cur.execute("SELECT COUNT(*) FROM price_orders WHERE status IN ('open','untriggered')")
        open_po = cur.fetchone()[0]
        # count reduce-only POs when flat
        flat_syms = set()
        held = {p["contract"] for p in positions}
        cur.execute("SELECT contract, status, reduce_only FROM price_orders WHERE status IN ('open','untriggered')")
        for c, st, ro in cur.fetchall():
            if c not in held:
                orphan += 1
        con.close()
    except Exception as e:
        print(f"{bid:22} ERR {e}")
        continue
    pnl_pct = (total - INIT) / INIT * 100 if total else 0
    flag = ""
    if dup:
        flag += " DUP"
    if total < INIT * 0.85 or total < 0:
        flag += " DD"
    if max_notional > INIT * 3:
        flag += " SIZE"
    if orphan:
        flag += " ORPHAN"
    print(f"{bid:22} {total:10.2f} {pnl_pct:+7.2f}% {dup:8} {max_notional:12.0f} {pos_n:5} {orphan:8}{flag}")
    rows.append((bid, total, dup, max_notional, pos_n, orphan, flag))

print("=" * 88)
bad = [r for r in rows if r[-1]]
if bad:
    print("异常标记:", ", ".join(f"{r[0]}({r[-1].strip()})" for r in bad))
else:
    print("未发现重复成交 / 巨额回撤 / 超仓 / 孤儿单标记")
