# -*- coding: utf-8 -*-
"""底仓触发链路本地真数据测试（2026-11-03）。

把引擎真实方法跑在 N 个历史时点上（每 12h 一个窗口）：
  4H ST/EMA55 定方向与闸门 → 240×15m 极值 → SMC 结构吸附 → 触发价对比。
数据：Gate 真实 15m/4h K 线；每次窗口把切片写入临时 kline.db 后调真实方法。
"""
import json
import os
import sqlite3
import sys
import tempfile
import urllib.request
from datetime import datetime, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.strategist.indicators import attach_indicators  # noqa: E402

os.environ.setdefault("no_proxy", "api.gateio.ws")


def fetch(interval, n):
    u = (f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
         f"?contract=ETH_USDT&interval={interval}&limit={n}")
    d = json.load(urllib.request.urlopen(u, timeout=30))
    return [{"t": int(x["t"]), "o": float(x["o"]), "h": float(x["h"]),
             "l": float(x["l"]), "c": float(x["c"]), "v": float(x["v"])} for x in d]


def write_db(db, rows, interval):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS kline (t INTEGER, o REAL, h REAL,"
                " l REAL, c REAL, v REAL, symbol TEXT, interval TEXT, closed INTEGER)")
    con.execute("DELETE FROM kline WHERE interval=?", (interval,))
    con.executemany(
        "INSERT INTO kline (t,o,h,l,c,v,symbol,interval,closed) VALUES (?,?,?,?,?,?,?,?,1)",
        [(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"], "ETH_USDT", interval)
         for b in rows])
    con.commit()
    con.close()


def main():
    bars15 = fetch("15m", 999)
    bars4h = fetch("4h", 250)
    tmp = tempfile.mkdtemp()
    db = str(Path(tmp) / "kline.db")

    ex = Executor.__new__(Executor)
    ex.ladder_template = {"levels_dynamic": {"db_path": db}}

    dyn15 = {"interval": "15m", "db_path": db}
    snap_cfg = {"interval": "15m", "bars": 300, "tol_pct": 0.5}
    st_cfg = {"ema_len": 55}

    print(f"{'as-of(UTC)':>17} {'mark':>8} {'4H方向':>6} {'ST闸门':>12} "
          f"{'side':>5} {'裸极值':>8} {'吸附后':>8} {'结构':>14} {'shift%':>7} {'dist%':>6}")
    print("-" * 108)

    n_windows = 9
    for w in range(n_windows):
        off15 = w * 48                      # 12h = 48 根 15m
        off4 = w * 3                        # 12h = 3 根 4h
        c15 = bars15[: len(bars15) - off15]
        c4 = bars4h[: len(bars4h) - off4]
        if len(c15) < 300 or len(c4) < 70:
            continue
        asof = datetime.fromtimestamp(c15[-1]["t"], UTC).strftime("%m-%d %H:%M")
        mark = c15[-1]["c"]
        write_db(db, c15[-300:], "15m")
        write_db(db, c4[-70:], "4h")

        # 4h 方向 + st_filter（真实方法）
        rows4 = [dict(r) for r in c4[-70:]]
        attach_indicators(rows4, ["ema55", "supertrend10_dir"])
        dir4 = rows4[-1].get("supertrend10_dir")
        side = "short" if dir4 == 1 else ("long" if dir4 == -1 else "?")
        ok, fn = ex._base_pos_st_filter("ETH_USDT", side, st_cfg, mark)
        gate = "PASS" if ok else ("EMA✗" if fn.get("dir_ok") else "DIR✗")

        # 裸极值（真实 _range_edges，240 根）
        lo, hi, _ = ex._range_edges("ETH_USDT", mark, dyn15, {"lookback": 240})
        raw = hi if side == "short" else lo
        dist = abs(raw - mark) / mark * 100 if raw and mark else 0.0

        # min_dist_pct 闸门（引擎：dist < 1.0 = 已在底部区 → 本次不建）
        if dist < 1.0:
            print(f"{asof:>17} {mark:>8.2f} {'跌' if dir4==1 else '涨':>6} {gate:>12} "
                  f"{side:>5} {raw:>8.2f} {'—':>8} {'dist<1.0不建':>14} {'-':>7} {dist:>6.2f}")
            continue

        # 结构吸附（真实方法）
        if raw > 0:
            snapped, sn = ex._base_pos_structure_snap(
                "ETH_USDT", side, float(raw), snap_cfg, mark)
            struct = f"{sn['structure']:.1f}" if sn.get("snapped") else "保留裸值"
            shift = f"{sn.get('shift_pct', 0):+.2f}" if sn.get("snapped") else "-"
        else:
            snapped, struct, shift = 0.0, "n/a", "-"

        print(f"{asof:>17} {mark:>8.2f} {'跌' if dir4==1 else '涨':>6} {gate:>12} "
              f"{side:>5} {raw:>8.2f} {snapped:>8.2f} {struct:>14} {shift:>7} {dist:>6.2f}")

    print("-" * 108)
    print("说明：dist% = 裸极值距现价（min_dist_pct=1.0 闸门）；吸附只朝成交更容易方向，"
          "无结构保留裸值（非阻断）。")


if __name__ == "__main__":
    main()
