# -*- coding: utf-8 -*-
import sqlite3
from pathlib import Path

print(f"{'bot':22} {'orphanSLTP':>10} {'entryPO':>8}")
for db in sorted(Path("data/bots").glob("*/paper/account.db")):
    bid = db.parents[1].name
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("SELECT contract FROM positions WHERE ABS(size)>0")
    held = {r[0] for r in cur.fetchall()}
    cur.execute(
        "SELECT contract, reduce_only FROM price_orders WHERE status IN ('open','untriggered')"
    )
    orphan = entry = 0
    for c, ro in cur.fetchall():
        if c not in held:
            if int(ro or 0) == 1:
                orphan += 1
            else:
                entry += 1
    con.close()
    if orphan or entry:
        print(f"{bid:22} {orphan:10} {entry:8}")
