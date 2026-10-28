#!/usr/bin/env python3
"""自愈：检测 kline 指标列 NULL 并重算（systemd timer 每 15 分钟跑一次）。

为什么需要：legacy 写入器会整行覆盖、旧进程可能带旧代码、回补与实时写入会打架 ——
任何一条路径都可能让指标列静默变 NULL（实测 2026-10-28 夜间 8 小时 3115/3140 行全 NULL，
没人发现）。与其依赖人工巡检，不如让「NULL > 0」自动触发重算。
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recalc  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402

DB = Path(__file__).resolve().parent / "data" / "kline.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    specs = parse_indicators(None)
    cols = [c for s in specs for c in s.columns]
    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")

    bad: list[tuple[str, str, int]] = []
    expr = " + ".join(f"([{c}] IS NULL)" for c in cols)
    for sym, iv in con.execute("SELECT DISTINCT symbol, interval FROM kline").fetchall():
        row = con.execute("SELECT t FROM kline WHERE symbol=? AND interval=? "
                          "ORDER BY t ASC LIMIT 1 OFFSET 199", (sym, iv)).fetchone()
        if not row:
            continue
        # 只看已收盘行；未收盘 bar 没指标是设计如此
        n = con.execute(f"SELECT sum({expr}) FROM kline WHERE symbol=? AND interval=? "
                        f"AND t>=? AND closed=1", (sym, iv, row[0])).fetchone()[0] or 0
        if n:
            bad.append((sym, iv, int(n)))

    if not bad:
        con.close()
        if not args.quiet:
            print("OK: 无 NULL")
        return 0

    t0 = time.time()
    total = 0
    for sym, iv, _ in bad:
        total += recalc.recalc_series(con, sym, iv, specs, table="kline")["updated"]
    con.commit()

    left = 0
    for sym, iv, _ in bad:
        row = con.execute("SELECT t FROM kline WHERE symbol=? AND interval=? "
                          "ORDER BY t ASC LIMIT 1 OFFSET 199", (sym, iv)).fetchone()
        if not row:
            continue
        left += con.execute(f"SELECT sum({expr}) FROM kline WHERE symbol=? AND interval=? "
                            f"AND t>=? AND closed=1", (sym, iv, row[0])).fetchone()[0] or 0
    con.close()

    print(json.dumps({"repaired_series": len(bad), "rows": total,
                      "remaining_nulls": int(left), "secs": round(time.time() - t0, 1)},
                     ensure_ascii=False))
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
