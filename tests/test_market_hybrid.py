import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.indicators import (  # noqa: E402
    attach_indicators,
    atr,
    ema,
    latest_indicators,
    rsi,
)
from gate_bot.strategist.market import (  # noqa: E402
    MarketConfig,
    is_stale,
    load_local_candles,
    resolve_db_path,
    resolve_candles,
)
from gate_bot.strategist.snapshot import collect_snapshot  # noqa: E402
from gate_bot.gate_client import GateApiError  # noqa: E402


class TestIndicators(unittest.TestCase):
    def test_ema_seeded_with_sma(self):
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        out = ema(closes, 3)
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 2.0)  # SMA(1,2,3)
        # k=0.5 → 4*0.5 + 2*0.5 = 3
        self.assertAlmostEqual(out[3], 3.0)
        self.assertAlmostEqual(out[4], 4.0)

    def test_ema_short_series(self):
        self.assertEqual(ema([1.0, 2.0], 5), [None, None])

    def test_rsi_all_gains(self):
        closes = [float(i) for i in range(1, 20)]  # strictly up
        out = rsi(closes, 14)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[14], 100.0)
        self.assertAlmostEqual(out[-1], 100.0)

    def test_rsi_mixed(self):
        closes = [10, 11, 10.5, 11.5, 11.0, 12.0, 11.5, 12.5, 12.0, 13.0,
                  12.5, 13.5, 13.0, 14.0, 13.5, 14.5, 14.0, 15.0, 14.5]
        out = rsi([float(x) for x in closes], 14)
        self.assertIsNone(out[13])
        val = out[14]
        self.assertIsNotNone(val)
        self.assertTrue(0.0 < val < 100.0)

    def test_atr_wilder(self):
        highs = [10.0, 12.0, 11.0, 13.0, 12.5, 14.0]
        lows = [8.0, 9.0, 9.5, 10.0, 11.0, 11.5]
        closes = [9.0, 11.0, 10.0, 12.0, 12.0, 13.0]
        out = atr(highs, lows, closes, 3)
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertIsNone(out[2])
        # i=3 first ATR = mean of TR[1..3]
        # TR1=max(3,|12-9|,|9-9|)=3; TR2=max(1.5,|11-11|,|9.5-11|)=1.5; TR3=max(3,|13-10|,|10-10|)=3
        self.assertAlmostEqual(out[3], (3 + 1.5 + 3) / 3)
        self.assertIsNotNone(out[4])

    def test_attach_keeps_db_ema20(self):
        rows = []
        for i in range(60):
            c = 1.0 + i * 0.01
            rows.append(
                {
                    "t": i,
                    "o": 1,
                    "h": c + 0.1,
                    "l": c - 0.1,
                    "c": c,
                    "v": 1,
                    "ema20": 99.0 if i == 0 else None,
                    "atr14": None,
                }
            )
        out = attach_indicators(rows, ["ema20", "ema50", "rsi14"])
        self.assertEqual(out[0]["ema20"], 99.0)  # DB value kept
        self.assertIsNotNone(out[-1].get("ema50"))
        self.assertIn("rsi14", out[-1])

    def test_attach_warm_starts_from_db_ema(self):
        """Gap after a DB ema20 must continue from that state, not reseed SMA."""
        rows = []
        for i in range(5):
            c = float(i + 1)
            rows.append(
                {
                    "t": i,
                    "o": c,
                    "h": c + 1,
                    "l": c - 1,
                    "c": c,
                    "v": 1,
                    "ema20": 100.0 if i == 3 else None,
                    "atr14": None,
                }
            )
        out = attach_indicators(list(rows), ["ema20"])
        self.assertEqual(out[3]["ema20"], 100.0)
        k = 2.0 / 21.0
        self.assertAlmostEqual(out[4]["ema20"], 5.0 * k + 100.0 * (1 - k))

    def test_null_close_does_not_zero_series(self):
        rows = [
            {"t": 0, "h": 2, "l": 0, "c": 1.0, "ema20": 1.0},
            {"t": 1, "h": None, "l": None, "c": None},
            {"t": 2, "h": 3, "l": 1, "c": 2.0},
        ]
        out = attach_indicators(rows, ["ema20", "atr14", "rsi14"])
        self.assertIsNone(out[1]["ema20"])
        self.assertIsNone(out[1]["atr14"])
        # continues from DB state at row0 across the gap when close resumes
        k = 2.0 / 21.0
        self.assertAlmostEqual(out[2]["ema20"], 2.0 * k + 1.0 * (1 - k))

    def test_rsi_hand_fixture(self):
        # First 14 changes are pure losses → RSI=0 at index 14; later gains lift it.
        closes = [20.0, 19.0, 18.0, 17.0, 16.0, 15.0, 14.0, 13.0, 12.0, 11.0,
                  10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 6.0, 7.0, 8.0]
        out = rsi(closes, 14)
        self.assertAlmostEqual(out[14], 0.0)
        self.assertGreater(out[-1], 0.0)
        self.assertLess(out[-1], 100.0)

    def test_latest_indicators(self):
        rows = [{"ema20": 1.0, "ema50": 2.0, "atr14": 3.0, "rsi14": 4.0}]
        self.assertEqual(
            latest_indicators(rows, ["ema20", "ema50", "atr14", "rsi14"]),
            {"ema20": 1.0, "ema50": 2.0, "atr14": 3.0, "rsi14": 4.0},
        )


class FakeClient:
    def __init__(self, rest_rows=None, last=100.0, fail_rest=False, fail_account=False):
        self.rest_rows = rest_rows or []
        self.last = last
        self.fail_rest = fail_rest
        self.fail_account = fail_account
        self.rest_calls = 0

    def public_get(self, path, qs=""):
        if "candlesticks" in path:
            self.rest_calls += 1
            if self.fail_rest:
                raise GateApiError("rest down")
            return self.rest_rows
        raise GateApiError(f"unexpected {path}")

    def get_last_price(self, symbol):
        return self.last

    def get_account(self):
        if self.fail_account:
            raise GateApiError("account down")
        return {"position_mode": "single", "available": "100", "total": "100"}

    def get_positions(self):
        return []


class TestMarket(unittest.TestCase):
    def _db(self, tmp: Path, rows, env="live"):
        data = tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        name = "kline_testnet.db" if env == "testnet" else "kline.db"
        db = data / name
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kline ("
            "t INTEGER, symbol TEXT, interval TEXT, o TEXT, h TEXT, l TEXT, c TEXT, "
            "v TEXT, sum TEXT, ema20 TEXT, atr14 TEXT)"
        )
        for r in rows:
            conn.execute(
                "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["t"], r["symbol"], r["interval"], str(r["o"]), str(r["h"]), str(r["l"]),
                    str(r["c"]), str(r["v"]), str(r.get("sum", 0)), r.get("ema20"), r.get("atr14"),
                ),
            )
        conn.commit()
        conn.close()
        return db

    def test_resolve_db_path_testnet(self):
        cfg = MarketConfig(mode="hybrid", pa_data_root="X")
        p = resolve_db_path("testnet", cfg)
        self.assertTrue(str(p).endswith("kline_testnet.db"))

    def test_load_local_candles(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            now = int(time.time())
            db = self._db(
                tmp,
                [
                    {"t": now - 900, "symbol": "BTC_USDT", "interval": "15m", "o": 1, "h": 2, "l": 0.5,
                     "c": 1.5, "v": 1, "ema20": "1.4", "atr14": "0.2"},
                    {"t": now, "symbol": "BTC_USDT", "interval": "15m", "o": 1, "h": 2, "l": 0.5,
                     "c": 1.6, "v": 1, "ema20": "1.5", "atr14": "0.2"},
                ],
            )
            rows = load_local_candles(db, "BTC_USDT", "15m", 10)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["t"], now - 900)
            self.assertAlmostEqual(rows[0]["ema20"], 1.4)

    def test_is_stale(self):
        now = 1_700_000_000.0
        rows = [{"t": now - 100}]
        self.assertFalse(is_stale(rows, "15m", 2.0, now=now))
        rows_old = [{"t": now - 3600}]
        self.assertTrue(is_stale(rows_old, "15m", 2.0, now=now))
        self.assertTrue(is_stale([], "15m", 2.0, now=now))
        # milliseconds epoch must not look "fresh forever"
        ms_old = [{"t": (now - 3600) * 1000}]
        self.assertTrue(is_stale(ms_old, "15m", 2.0, now=now))

    def test_hybrid_uses_local_when_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            now = int(time.time())
            self._db(
                tmp,
                [
                    {"t": now - 10, "symbol": "BTC_USDT", "interval": "15m", "o": 1, "h": 2, "l": 0.5,
                     "c": 1.5, "v": 1, "ema20": "1.4", "atr14": "0.2"}
                ]
                * 30,
            )
            # fix unique times
            db = tmp / "data" / "kline.db"
            conn = sqlite3.connect(db)
            conn.execute("DELETE FROM kline")
            for i in range(30):
                conn.execute(
                    "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (now - (30 - i) * 900, "BTC_USDT", "15m", "1", "2", "0.5", "1.5", "1", "0",
                     "1.4", "0.2"),
                )
            conn.commit()
            conn.close()
            client = FakeClient()
            res = resolve_candles(
                client,
                "BTC_USDT",
                "15m",
                30,
                MarketConfig(mode="hybrid", pa_data_root=str(tmp / "data")),
                env="live",
            )
            self.assertEqual(res.source, "local")
            self.assertFalse(res.stale)
            self.assertEqual(client.rest_calls, 0)
            self.assertEqual(len(res.rows), 30)

    def test_hybrid_stale_falls_back_to_rest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            now = int(time.time())
            self._db(
                tmp,
                [
                    {"t": now - 100000, "symbol": "BTC_USDT", "interval": "15m", "o": 1, "h": 2,
                     "l": 0.5, "c": 1.5, "v": 1, "ema20": "1.4", "atr14": "0.2"}
                ],
            )
            rest_rows = [[now - 900, "1", "1.5", "2", "0.5", "1", "0"] for _ in range(5)]
            rest_rows.append([now, "1", "1.6", "2", "0.5", "1", "0"])
            client = FakeClient(rest_rows=rest_rows)
            res = resolve_candles(
                client,
                "BTC_USDT",
                "15m",
                10,
                MarketConfig(mode="hybrid", pa_data_root=str(tmp / "data")),
                env="live",
            )
            self.assertEqual(res.source, "exchange")
            self.assertEqual(client.rest_calls, 1)
            self.assertIn("candles_stale", res.degraded)

    def test_hybrid_missing_db_rest_only(self):
        client = FakeClient(rest_rows=[[int(time.time()), "1", "1", "1", "1", "1", "0"]])
        res = resolve_candles(
            client,
            "BTC_USDT",
            "15m",
            5,
            MarketConfig(mode="hybrid", pa_data_root="/no/such/dir"),
            env="live",
        )
        self.assertEqual(res.source, "exchange")
        self.assertIn("local_db", res.degraded)

    def test_local_only_never_rest(self):
        client = FakeClient()
        res = resolve_candles(
            client,
            "BTC_USDT",
            "15m",
            5,
            MarketConfig(mode="local_only", pa_data_root="/no/such/dir"),
            env="live",
        )
        self.assertEqual(client.rest_calls, 0)
        self.assertEqual(res.source, "local")
        self.assertIsNotNone(res.error)

    def test_rest_only_default(self):
        client = FakeClient(rest_rows=[[int(time.time()), "1", "1", "1", "1", "1", "0"]])
        res = resolve_candles(client, "BTC_USDT", "15m", 5, MarketConfig(), env="live")
        self.assertEqual(res.source, "exchange")
        self.assertEqual(client.rest_calls, 1)

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            MarketConfig(mode="nope")


class TestSnapshot(unittest.TestCase):
    def test_snapshot_meta_and_indicators(self):
        now = int(time.time())
        rows = [[now - (30 - i) * 900, "1", str(1 + i * 0.01), "2", "0.5", "1", "0"] for i in range(30)]
        client = FakeClient(rest_rows=rows)
        snap = collect_snapshot(client, ["BTC_USDT"], candles=30, interval="15m")
        entry = snap["market"]["BTC_USDT"]
        self.assertIn("candles", entry)
        self.assertIn("last", entry)
        self.assertEqual(entry["candle_source"], "exchange")
        self.assertIn("indicators", entry)
        self.assertIn("market_mode", snap["meta"])
        self.assertEqual(snap["meta"]["market_mode"], "rest_only")

    def test_account_error_flag(self):
        client = FakeClient(fail_account=True)
        snap = collect_snapshot(client, ["BTC_USDT"], candles=5, interval="15m")
        self.assertIn("error", snap["account"])
        self.assertIn("account", snap["meta"]["degraded"])

    def test_local_hybrid_meta(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            now = int(time.time())
            data = tmp / "data"
            data.mkdir()
            db = data / "kline.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE kline (t INTEGER, symbol TEXT, interval TEXT, o TEXT, h TEXT, l TEXT, "
                "c TEXT, v TEXT, sum TEXT, ema20 TEXT, atr14 TEXT)"
            )
            for i in range(40):
                conn.execute(
                    "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (now - (40 - i) * 900, "BTC_USDT", "15m", "1", "2", "0.5", "1.5", "1", "0",
                     "1.4", "0.2"),
                )
            conn.commit()
            conn.close()
            client = FakeClient()
            snap = collect_snapshot(
                client,
                ["BTC_USDT"],
                candles=30,
                interval="15m",
                market_cfg=MarketConfig(mode="hybrid", pa_data_root=str(data)),
                env="live",
            )
            self.assertEqual(snap["meta"]["candle_source"]["BTC_USDT"], "local")
            self.assertEqual(client.rest_calls, 0)
            self.assertEqual(snap["market"]["BTC_USDT"]["candles"][0]["ema20"], 1.4)


class TestPlanRunnerAccountAbort(unittest.TestCase):
    def test_account_unavailable_aborts(self):
        import tempfile

        from gate_bot.strategist.loop import PlanRunner, StrategistConfig

        class BoomClient(FakeClient):
            def __init__(self):
                super().__init__(fail_account=True)

        class NeverLLM:
            def chat(self, system, user):
                raise AssertionError("LLM must not be called when account unavailable")

        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"
            inbox.mkdir()
            cfg = StrategistConfig(symbols=["BTC_USDT"], candles=5, timeframe="15m", write_hold=True)
            runner = PlanRunner(BoomClient(), cfg, inbox, Path(td) / "hist", llm=NeverLLM())
            result = runner.run_once()
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("error"), "account_unavailable")
            self.assertEqual(list(inbox.glob("*.json")), [])

    def test_positions_fail_still_aborts_but_keeps_available(self):
        class PosFailClient(FakeClient):
            def get_positions(self):
                raise GateApiError("positions down")

        snap = collect_snapshot(PosFailClient(), ["BTC_USDT"], candles=5, interval="15m")
        self.assertIn("error", snap["account"])
        self.assertEqual(snap["account"].get("available"), "100")


if __name__ == "__main__":
    unittest.main()
