# -*- coding: utf-8 -*-
"""读取侧 v2 适配：动态指标列 / closed 过滤 / 短数据降级 / 两形态解析 / carried 动态化。

对应方案 §5.2 的 1~5 项与 §7 的 hybrid 短数据回退。
"""
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.indicators import (attach_indicators, carried_keys,  # noqa: E402
                                            strip_carried_indicators)
from omnialpha.strategist.market import (MarketConfig, _normalize_candle_rows,  # noqa: E402
                                         load_local_candles, load_local_candles_ex,
                                         resolve_candles)

NOW = int(time.time())


def make_v2_db(tmp: Path, rows, name="kline.db") -> Path:
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = data / name
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER,"
        " o REAL, h REAL, l REAL, c REAL, v REAL, sum REAL,"
        " closed INTEGER NOT NULL DEFAULT 1, src TEXT NOT NULL DEFAULT 'ws',"
        " ema20 REAL, ema200 REAL, rsi14 REAL,"
        " PRIMARY KEY (symbol, interval, t)) WITHOUT ROWID")
    for r in rows:
        conn.execute(
            "INSERT INTO kline(symbol,interval,t,o,h,l,c,v,sum,closed,src,ema20,ema200,rsi14)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["symbol"], r["interval"], r["t"], r["o"], r["h"], r["l"], r["c"],
             r["v"], 0.0, int(r.get("closed", 1)), r.get("src", "ws"),
             r.get("ema20"), r.get("ema200"), r.get("rsi14")))
    conn.commit()
    conn.close()
    return db


def rows_for(n=30, interval="15m", start=None, **over):
    start = start or (NOW - n * 900)
    out = []
    for i in range(n):
        r = {"symbol": "ETH_USDT", "interval": interval, "t": start + i * 900,
             "o": 2400 + i, "h": 2402 + i, "l": 2398 + i, "c": 2401 + i, "v": 10.0,
             "ema20": 2400.0 + i, "ema200": 2450.0, "rsi14": 55.0}
        r.update(over)
        out.append(r)
    return out


class FakeClient:
    def __init__(self, rest_rows=None, fail=False):
        self.rest_rows = rest_rows if rest_rows is not None else []
        self.fail = fail
        self.calls = []

    def get_klines(self, symbol, interval, limit):
        self.calls.append((symbol, interval, limit))
        if self.fail:
            raise RuntimeError("rest down")
        return [{k: v for k, v in r.items()} for r in self.rest_rows[-limit:]]


class TestLoadLocalV2(unittest.TestCase):
    def test_dynamic_indicator_columns(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_v2_db(Path(td), rows_for(30))
            res = load_local_candles_ex(db, "ETH_USDT", "15m", 30)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.version, 2)
            row = res.rows[-1]
            self.assertIn("ema200", row)       # 动态列被带出来
            self.assertIn("rsi14", row)
            self.assertIsNotNone(row["ema200"])

    def test_closed_filter(self):
        rows = rows_for(10)
        rows[-1]["closed"] = 0   # 未收线
        with tempfile.TemporaryDirectory() as td:
            db = make_v2_db(Path(td), rows)
            res = load_local_candles_ex(db, "ETH_USDT", "15m", 10)
            self.assertEqual(res.returned, 9)
            self.assertTrue(all(r["t"] != rows[-1]["t"] for r in res.rows))

    def test_short_status(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_v2_db(Path(td), rows_for(10))
            res = load_local_candles_ex(db, "ETH_USDT", "15m", 50)
            self.assertEqual(res.status, "short")
            self.assertTrue(res.usable)

    def test_missing_and_empty(self):
        self.assertEqual(load_local_candles_ex(Path("/no/such.db"), "E", "15m", 10).status, "missing")
        with tempfile.TemporaryDirectory() as td:
            db = make_v2_db(Path(td), [])
            self.assertEqual(load_local_candles_ex(db, "ETH_USDT", "15m", 10).status, "empty")

    def test_legacy_signature_still_returns_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = make_v2_db(Path(td), rows_for(5))
            rows = load_local_candles(db, "ETH_USDT", "15m", 5)
            self.assertEqual(len(rows), 5)


class TestHybridShortFallback(unittest.TestCase):
    def _cfg(self, tmp):
        # 库建在 <tmp>/data 下，pa_data_root 指向该目录
        return MarketConfig(mode="hybrid", pa_data_root=str(Path(tmp) / "data"))

    def test_short_falls_back_to_rest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            make_v2_db(tmp, rows_for(10))
            rest = rows_for(60, start=NOW - 60 * 900)
            client = FakeClient(rest_rows=rest)
            res = resolve_candles(client, "ETH_USDT", "15m", 50,
                                  market_cfg=self._cfg(tmp), bot_root=tmp)
            self.assertEqual(res.source, "exchange")
            self.assertIn("local_short", res.degraded)
            self.assertEqual(len(client.calls), 1)

    def test_short_and_rest_failure_returns_local(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            make_v2_db(tmp, rows_for(10))
            client = FakeClient(fail=True)
            res = resolve_candles(client, "ETH_USDT", "15m", 50,
                                  market_cfg=self._cfg(tmp), bot_root=tmp)
            self.assertEqual(res.source, "local")
            self.assertTrue(res.stale)
            self.assertIn("local_short", res.degraded)
            self.assertEqual(len(res.rows), 10)

    def test_full_local_no_rest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            make_v2_db(tmp, rows_for(40))
            client = FakeClient(fail=True)
            res = resolve_candles(client, "ETH_USDT", "15m", 30,
                                  market_cfg=self._cfg(tmp), bot_root=tmp)
            self.assertEqual(res.source, "local")
            self.assertEqual(client.calls, [])


class TestCarriedDynamic(unittest.TestCase):
    def test_carried_keys_detects_v2_columns(self):
        rows = rows_for(5)
        self.assertEqual(sorted(carried_keys(rows)), ["ema20", "ema200", "rsi14"])

    def test_strip_removes_all_indicator_columns_when_disabled(self):
        rows = attach_indicators(list(rows_for(5)), [])
        out = strip_carried_indicators(rows, [])
        for col in ("ema20", "ema200", "rsi14"):
            self.assertNotIn(col, out[0])
        self.assertIn("c", out[0])

    def test_strip_keeps_requested(self):
        out = strip_carried_indicators(list(rows_for(5)), ["ema200"])
        self.assertIn("ema200", out[0])
        self.assertNotIn("rsi14", out[0])

    def test_attach_keeps_db_value(self):
        rows = rows_for(5)
        rows[0]["ema20"] = 999.0
        out = attach_indicators(rows, ["ema20"])
        self.assertEqual(out[0]["ema20"], 999.0)


class TestNormalizeRows(unittest.TestCase):
    def test_accepts_dict_and_list(self):
        as_dict = [{"t": NOW, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3, "sum": 4}]
        as_list = [[NOW, 3, 1.5, 2, 0.5, 1, 4]]  # Gate 原生 [t,v,c,h,l,o,sum]
        a = _normalize_candle_rows(as_dict, 10)
        b = _normalize_candle_rows(as_list, 10)
        self.assertEqual(a[0]["c"], b[0]["c"])
        self.assertEqual(a[0]["o"], b[0]["o"])
        self.assertEqual(a[0]["h"], b[0]["h"])

    def test_normalizes_ms_timestamp(self):
        rows = [{"t": NOW * 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]
        self.assertEqual(_normalize_candle_rows(rows, 10)[0]["t"], NOW)

    def test_sum_alias_a(self):
        rows = [{"t": NOW, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "a": 7}]
        self.assertEqual(_normalize_candle_rows(rows, 10)[0]["sum"], 7)


if __name__ == "__main__":
    unittest.main()
