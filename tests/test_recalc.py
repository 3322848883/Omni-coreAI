# -*- coding: utf-8 -*-
"""流式重算 recalc_series / verify_series（pa-data-source/recalc.py）。

验收点
1. 全序列重算后，库内值与「批量函数直算」逐位一致；
2. `from_t` 增量重算只写受影响区间，结果与全量重算一致；
3. v1 库（只有 ema20/atr14）不因配置多了列而报错，缺失列被如实报告。
"""
import random
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

import indicators_core as core  # noqa: E402
import schema_v2 as sv2  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402
from recalc import recalc_series, verify_series  # noqa: E402

SPECS = parse_indicators([{"ema": [20, 50, 200]}, {"atr": [14]}, {"rsi": [14]}])
COLS = [c for s in SPECS for c in s.columns]


def make_bars(n=800, seed=21):
    rnd = random.Random(seed)
    price = 2400.0
    bars = []
    for i in range(n):
        o = price
        c = max(1.0, o + rnd.gauss(0, 1.5))
        h = max(o, c) + abs(rnd.gauss(0, 0.7))
        l = min(o, c) - abs(rnd.gauss(0, 0.7))
        bars.append({"t": 1_700_000_000 + i * 900, "o": o, "h": h, "l": l, "c": c,
                     "v": 100.0 + rnd.random()})
        price = c
    return bars


def build_db(bars, with_cols=True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    sv2.init_v2(conn)
    if with_cols:
        sv2.ensure_indicator_columns(conn, SPECS)
    conn.executemany(
        "INSERT INTO kline(symbol,interval,t,o,h,l,c,v,sum,closed,src) "
        "VALUES ('ETH_USDT','15m',?,?,?,?,?,?,0,1,'ws')",
        [(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars],
    )
    conn.commit()
    return conn


def stored(conn, col):
    return [r[0] for r in conn.execute(
        f"SELECT [{col}] FROM kline WHERE symbol='ETH_USDT' AND interval='15m' ORDER BY t")]


class TestFullRecalc(unittest.TestCase):
    def test_matches_batch_bit_exact(self):
        bars = make_bars()
        conn = build_db(bars)
        res = recalc_series(conn, "ETH_USDT", "15m", SPECS)
        self.assertEqual(res["missing"], [])
        self.assertEqual(res["updated"], len(bars))
        c = [b["c"] for b in bars]
        h = [b["h"] for b in bars]
        l = [b["l"] for b in bars]
        expect = {"ema20": core.ema(c, 20), "ema50": core.ema(c, 50), "ema200": core.ema(c, 200),
                  "atr14": core.atr(h, l, c, 14), "rsi14": core.rsi(c, 14)}
        for col, series in expect.items():
            self.assertEqual(stored(conn, col), series, f"{col} 不一致")
        v = verify_series(conn, "ETH_USDT", "15m", SPECS, tail=300)
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["max_rel"], 0.0)

    def test_dry_run_writes_nothing(self):
        bars = make_bars(200)
        conn = build_db(bars)
        res = recalc_series(conn, "ETH_USDT", "15m", SPECS, dry_run=True)
        self.assertGreater(res["updated"], 0)
        self.assertTrue(all(x is None for x in stored(conn, "ema20")))


class TestIncrementalRecalc(unittest.TestCase):
    def test_from_t_matches_full(self):
        # 序列长度必须 > 预热（ema200 → 1005），否则 from_t 退化成全序列重算
        bars = make_bars(1300)
        conn = build_db(bars)
        recalc_series(conn, "ETH_USDT", "15m", SPECS)

        # 改最后 30 根的收盘价（模拟校准纠错）
        tail_t = bars[-30]["t"]
        conn.executemany(
            "UPDATE kline SET c=c+5, h=h+5, l=l+5 WHERE t=?", [(b["t"],) for b in bars[-30:]])
        conn.commit()
        res = recalc_series(conn, "ETH_USDT", "15m", SPECS, from_t=tail_t)
        self.assertIsNotNone(res["start_t"])
        self.assertLess(res["start_t"], tail_t)
        # 只重算「30 根 + 预热」而不是整条序列
        self.assertLess(res["updated"], len(bars))

        # 与「全量重算」结果比对：把 conn 的**当前数据**另存到一个干净库做全量重算
        conn2 = sqlite3.connect(":memory:")
        sv2.init_v2(conn2)
        sv2.ensure_indicator_columns(conn2, SPECS)
        conn2.executemany(
            "INSERT INTO kline(symbol,interval,t,o,h,l,c,v,sum,closed,src) "
            "VALUES ('ETH_USDT','15m',?,?,?,?,?,?,0,1,'ws')",
            conn.execute("SELECT t,o,h,l,c,v FROM kline WHERE symbol='ETH_USDT' "
                         "AND interval='15m' ORDER BY t").fetchall(),
        )
        conn2.commit()
        recalc_series(conn2, "ETH_USDT", "15m", SPECS)
        # 分级契约：
        #  - 有限记忆指标（ATR/RSI）预热够就**逐位相等**；
        #  - EMA 有无限记忆（k=2/(n+1) 衰减），从 warmup 起点重算只能逼近，
        #    实测相对误差 <1e-4（ema200 约 4e-7）。要 bit 级需从序列头重算。
        tol = {"ema20": 1e-9, "ema50": 1e-6, "ema200": 1e-4,
               "atr14": 0.0, "rsi14": 0.0}
        for col in COLS:
            a, b = stored(conn, col), stored(conn2, col)
            if tol[col] == 0.0:
                self.assertEqual(a, b, f"{col} 有限记忆指标应逐位相等")
                continue
            worst = 0.0
            for x, y in zip(a, b):
                if x is None or y is None:
                    continue
                worst = max(worst, 0.0 if x == y else abs(x - y) / max(abs(x), abs(y), 1e-12))
            self.assertLessEqual(worst, tol[col], f"{col} 增量与全量偏差 {worst:.2e}")

    def test_short_series_falls_back_to_full(self):
        """序列比预热短时 `start_t` 为 None：状态从序列头推，但**只写 from_t 之后**的行。"""
        bars = make_bars(400)
        conn = build_db(bars)
        recalc_series(conn, "ETH_USDT", "15m", SPECS)
        tail_t = bars[-1]["t"]
        conn.execute("UPDATE kline SET c=c+3, h=h+3, l=l+3 WHERE t=?", (tail_t,))
        conn.commit()
        res = recalc_series(conn, "ETH_USDT", "15m", SPECS, from_t=tail_t)
        self.assertIsNone(res["start_t"])
        self.assertEqual(res["updated"], 1)

        closes = [r[0] for r in conn.execute(
            "SELECT c FROM kline WHERE symbol='ETH_USDT' AND interval='15m' ORDER BY t")]
        expected = core.ema(closes, 20)[-1]
        self.assertEqual(stored(conn, "ema20")[-1], expected)


class TestV1Compatibility(unittest.TestCase):
    def test_missing_columns_reported_not_fatal(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE kline (t INTEGER, symbol TEXT, interval TEXT, o TEXT, h TEXT,"
            " l TEXT, c TEXT, v TEXT, sum TEXT, ema20 TEXT, atr14 TEXT,"
            " PRIMARY KEY (symbol, interval, t))")
        bars = make_bars(120)
        conn.executemany(
            "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)",
            [(b["t"], "ETH_USDT", "15m", b["o"], b["h"], b["l"], b["c"], b["v"], 0) for b in bars])
        conn.commit()
        res = recalc_series(conn, "ETH_USDT", "15m", SPECS)
        self.assertEqual(sorted(res["missing"]), sorted(
            [c for c in COLS if c not in ("ema20", "atr14")]))
        self.assertGreater(res["updated"], 0)
        self.assertIsNotNone(stored(conn, "ema20")[-1])
        self.assertIsNotNone(stored(conn, "atr14")[-1])


if __name__ == "__main__":
    unittest.main()
