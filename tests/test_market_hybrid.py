import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.indicators import (  # noqa: E402
    IndicatorNameError,
    attach_indicators,
    atr,
    boll,
    ema,
    latest_indicators,
    macd,
    parse_indicator_name,
    rsi,
    sma,
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

    def test_atr_pine_tr_true(self):
        """Pine ATR: ma(ta.tr(true), length); default RMA. tr[0]=h-l."""
        highs = [10.0, 12.0, 11.0, 13.0, 12.5, 14.0]
        lows = [8.0, 9.0, 9.5, 10.0, 11.0, 11.5]
        closes = [9.0, 11.0, 10.0, 12.0, 12.0, 13.0]
        out = atr(highs, lows, closes, 3)  # RMA default
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        # TR = [2, 3, 1.5, 3, 1.5, 2.5]; RMA seed idx2 = mean(2,3,1.5)
        self.assertAlmostEqual(out[2], (2 + 3 + 1.5) / 3)
        self.assertAlmostEqual(out[3], (out[2] * 2 + 3) / 3)
        self.assertIsNotNone(out[4])

    def test_atr_smoothing_variants(self):
        highs = [10.0, 12.0, 11.0, 13.0, 12.5, 14.0]
        lows = [8.0, 9.0, 9.5, 10.0, 11.0, 11.5]
        closes = [9.0, 11.0, 10.0, 12.0, 12.0, 13.0]
        # SMA of TR[0:3] at index 2
        sma_atr = atr(highs, lows, closes, 3, smoothing="sma")
        self.assertAlmostEqual(sma_atr[2], (2 + 3 + 1.5) / 3)
        # EMA / WMA 也应产出数值
        for sm in ("ema", "wma"):
            v = atr(highs, lows, closes, 3, smoothing=sm)
            self.assertIsNotNone(v[2], sm)

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

    def test_sma(self):
        out = sma([1.0, 2.0, 3.0, 4.0], 2)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[1], 1.5)
        self.assertAlmostEqual(out[3], 3.5)

    def test_macd_series(self):
        closes = [float(i) for i in range(1, 60)]
        m = macd(closes, 3, 6, 3)
        self.assertEqual(len(m["dif"]), 59)
        self.assertIsNotNone(m["dif"][-1])
        self.assertIsNotNone(m["dea"][-1])
        self.assertIsNotNone(m["hist"][-1])
        self.assertAlmostEqual(m["hist"][-1], m["dif"][-1] - m["dea"][-1])

    def test_boll_bands(self):
        closes = [10.0, 11.0, 9.0, 10.5, 10.0, 10.2, 9.8, 10.1, 10.0, 10.05]
        s = boll(closes, 5, 2.0)
        self.assertIsNotNone(s["middle"][-1])
        self.assertGreater(s["upper"][-1], s["middle"][-1])
        self.assertLess(s["lower"][-1], s["middle"][-1])

    def test_parse_indicator_names(self):
        self.assertEqual(parse_indicator_name("ma7")["kind"], "ma")
        self.assertEqual(parse_indicator_name("sma20")["period"], 20)
        self.assertEqual(parse_indicator_name("macd")["field"], "dif")
        self.assertEqual(parse_indicator_name("macd_dea")["field"], "dea")
        self.assertEqual(parse_indicator_name("macd_difference")["field"], "hist")
        self.assertEqual(parse_indicator_name("macd12_26_9")["fast"], 12)
        self.assertEqual(parse_indicator_name("boll20")["period"], 20)
        self.assertEqual(parse_indicator_name("boll20_2")["k"], 2.0)
        self.assertEqual(parse_indicator_name("boll_upper_band")["field"], "upper")

    def test_unknown_indicator_raises(self):
        with self.assertRaises(IndicatorNameError):
            parse_indicator_name("foo")
        with self.assertRaises(IndicatorNameError):
            attach_indicators([{"c": 1.0}], ["macd_cross", "zzz"])

    def test_attach_ma_macd_boll(self):
        rows = []
        for i in range(60):
            c = 1.0 + i * 0.01
            rows.append({"t": i, "o": c, "h": c + 0.1, "l": c - 0.1, "c": c, "v": 1.0})
        out = attach_indicators(rows, ["ma7", "sma20", "macd", "macd_dea", "macd_hist", "boll20"])
        last = out[-1]
        self.assertIsNotNone(last.get("ma7"))
        self.assertIsNotNone(last.get("sma20"))
        self.assertIsNotNone(last.get("macd"))
        self.assertIsNotNone(last.get("macd_dea"))
        self.assertIsNotNone(last.get("macd_hist"))
        self.assertIsNotNone(last.get("boll_upper"))
        self.assertIsNotNone(last.get("boll_middle"))
        self.assertIsNotNone(last.get("boll_lower"))

    def test_market_config_rejects_unknown_indicator(self):
        with self.assertRaises(ValueError):
            MarketConfig(mode="rest_only", indicators=["ema20", "foo"])

    def test_rsi_gap_uses_prev_close(self):
        rows = []
        for i in range(30):
            rows.append({"t": i, "o": 1, "h": 2, "l": 0, "c": 10.0 + i, "v": 1})
        rows[10]["c"] = None  # gap bar
        out = attach_indicators(rows, ["rsi7"])
        self.assertIsNone(out[10]["rsi7"])
        # after gap, series continues (change vs last known close)
        self.assertIsNotNone(out[12]["rsi7"])
        self.assertIsNotNone(out[-1]["rsi7"])

    def test_account_missing_available_errors(self):
        class NoAvail(FakeClient):
            def get_account(self):
                return {"position_mode": "single", "total": "1"}

        snap = collect_snapshot(NoAvail(), ["BTC_USDT"], candles=5, interval="15m")
        self.assertIn("error", snap["account"])
        self.assertIn("available", snap["account"]["error"])

    def test_custom_indicator_periods(self):
        rows = []
        for i in range(40):
            c = 1.0 + i * 0.01
            rows.append({"t": i, "o": c, "h": c + 0.1, "l": c - 0.1, "c": c, "v": 1})
        out = attach_indicators(rows, ["ema9", "ema21", "rsi7", "atr10"])
        self.assertIsNotNone(out[-1].get("ema9"))
        self.assertIsNotNone(out[-1].get("ema21"))
        self.assertIsNotNone(out[-1].get("rsi7"))
        self.assertIsNotNone(out[-1].get("atr10"))

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

    def get_contract(self, symbol):
        from gate_bot.gate_client import ContractMeta
        return ContractMeta(symbol, 0.0001, 1.0, 0.1, 100)

    def get_ticker(self, symbol):
        return {
            "contract": symbol,
            "last": str(self.last),
            "mark_price": str(self.last * 0.999),
            "index_price": str(self.last * 1.001),
            "funding_rate": "0.0001",
            "funding_rate_indicative": "0.00012",
            "high_24h": str(self.last * 1.02),
            "low_24h": str(self.last * 0.98),
            "change_percentage": "1.5",
            "change_price": "12.5",
            "volume_24h_quote": "123456",
            "highest_bid": str(self.last - 1),
            "lowest_ask": str(self.last + 1),
            "total_size": "999",
        }

    def get_contract_stats(self, symbol, limit=1):
        return [{
            "time": 1790000000,
            "open_interest": 111.0,
            "open_interest_usd": 222.0,
            "lsr_taker": 1.1,
            "lsr_account": 1.2,
            "top_lsr_account": 0.9,
            "long_liq_size": 0,
            "short_liq_size": 0,
            "mark_price": 99.5,
        }]

    def get_orderbook_top(self, symbol, limit=5):
        return {
            "bids": [{"p": "99", "s": "10"}, {"p": "98", "s": "20"}],
            "asks": [{"p": "101", "s": "11"}, {"p": "102", "s": "21"}],
            "current": 100,
        }

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

    def test_snapshot_contract_meta_for_ai(self):
        client = FakeClient(rest_rows=[[int(time.time()), "1", "1", "1", "1", "1", "0"]])
        snap = collect_snapshot(client, ["BTC_USDT"], candles=5, interval="15m")
        cm = snap["market"]["BTC_USDT"].get("contract")
        self.assertIsNotNone(cm)
        self.assertIn("quanto_multiplier", cm)
        self.assertIn("min_notional_usd", cm)
        self.assertGreater(cm["quanto_multiplier"], 0)

    def test_snapshot_multi_timeframe_extras(self):
        now = int(time.time())
        rows = [[now - (30 - i) * 900, "1", str(1 + i * 0.01), "2", "0.5", "1", "0"] for i in range(30)]
        client = FakeClient(rest_rows=rows)
        from gate_bot.strategist.market import MarketConfig

        snap = collect_snapshot(
            client,
            ["BTC_USDT"],
            candles=20,
            interval="15m",
            market_cfg=MarketConfig(
                mode="rest_only",
                extra_timeframes=["1h", "4h"],
                extra_candles=10,
            ),
        )
        tf = snap["market"]["BTC_USDT"].get("tf") or {}
        self.assertIn("1h", tf)
        self.assertIn("4h", tf)
        self.assertNotIn("15m", tf)  # primary is entry-level fields
        self.assertGreaterEqual(len(tf["1h"].get("candles") or []), 1)
        self.assertIn("indicators", tf["1h"])

    def test_snapshot_timeframes_and_indicators_all(self):
        now = int(time.time())
        rows = [[now - (40 - i) * 900, "1", str(1 + i * 0.01), "2", "0.5", "1", "0"] for i in range(40)]
        client = FakeClient(rest_rows=rows)
        from gate_bot.strategist.market import ALL_TIMEFRAMES, MarketConfig

        cfg = MarketConfig(mode="rest_only", extra_timeframes=["all"], indicators=["all"])
        self.assertEqual(set(cfg.extra_timeframes), set(ALL_TIMEFRAMES))
        self.assertIn("macd", cfg.indicators)
        self.assertIn("boll_upper", cfg.indicators)
        snap = collect_snapshot(client, ["BTC_USDT"], candles=15, interval="15m", market_cfg=cfg)
        tf = snap["market"]["BTC_USDT"].get("tf") or {}
        for t in ALL_TIMEFRAMES:
            if t == "15m":
                continue
            self.assertIn(t, tf)

    def test_snapshot_p1_ticker_stats_orderbook(self):
        client = FakeClient(rest_rows=[[int(time.time()), "1", "1", "1", "1", "1", "0"]])
        snap = collect_snapshot(
            client,
            ["BTC_USDT"],
            candles=5,
            interval="15m",
            market_cfg=MarketConfig(mode="rest_only", refresh=["ticker", "stats", "orderbook"]),
        )
        e = snap["market"]["BTC_USDT"]
        self.assertAlmostEqual(e["last"], 100.0)
        self.assertIn("funding_rate", e["ticker"])
        self.assertAlmostEqual(e["ticker"]["funding_rate"], 0.0001)
        self.assertIn("mark_price", e["ticker"])
        self.assertIn("index_price", e["ticker"])
        self.assertAlmostEqual(e["stats"]["open_interest"], 111.0)
        self.assertEqual(len(e["orderbook"]["bids"]), 2)
        self.assertEqual(e["orderbook"]["asks"][0]["p"], 101.0)
        self.assertIn("ticker", snap["meta"]["refresh"])

    def test_snapshot_refresh_subset(self):
        client = FakeClient(rest_rows=[[int(time.time()), "1", "1", "1", "1", "1", "0"]])
        snap = collect_snapshot(
            client,
            ["BTC_USDT"],
            candles=5,
            interval="15m",
            market_cfg=MarketConfig(mode="rest_only", refresh=["ticker"]),
        )
        e = snap["market"]["BTC_USDT"]
        self.assertIn("ticker", e)
        self.assertNotIn("stats", e)
        self.assertNotIn("orderbook", e)

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
    def test_account_unavailable_continues(self):
        """账户取不到 → 不中止，继续行情分析（新行为）。"""
        import tempfile

        from gate_bot.strategist.loop import PlanRunner, StrategistConfig

        class BoomClient(FakeClient):
            def __init__(self):
                super().__init__(fail_account=True)

        called = {"n": 0}

        class StubLLM:
            last_reasoning = ""
            last_reasoning_chain = []

            def chat(self, system, user):
                called["n"] += 1
                return '{"cycle_id":"c","reasoning":"行情","chips":[{"symbol":"BTC_USDT","action":"hold","confidence":0.5}]}'

        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"
            inbox.mkdir()
            cfg = StrategistConfig(symbols=["BTC_USDT"], candles=5, timeframe="15m", write_hold=True)
            runner = PlanRunner(BoomClient(), cfg, inbox, Path(td) / "hist", llm=StubLLM())
            result = runner.run_once()
            self.assertNotEqual(result.get("error"), "account_unavailable")
            self.assertGreater(called["n"], 0, "账户缺失时仍应调用 LLM 做行情分析")

    def test_positions_fail_still_aborts_but_keeps_available(self):
        class PosFailClient(FakeClient):
            def get_positions(self):
                raise GateApiError("positions down")

        snap = collect_snapshot(PosFailClient(), ["BTC_USDT"], candles=5, interval="15m")
        self.assertIn("error", snap["account"])
        self.assertEqual(snap["account"].get("available"), "100")


class VenueClient:
    """Minimal per-exchange adapter: get_klines is the venue REST source."""

    def __init__(self, name: str, rows):
        self.name = name
        self._rows = rows
        self.kline_calls = 0
        self.public_calls = 0

    def get_klines(self, symbol, interval, limit=100):
        self.kline_calls += 1
        return list(self._rows)[-int(limit) :]

    def public_get(self, path, qs=""):
        self.public_calls += 1
        raise AssertionError(f"{self.name} must not fall back to Gate public_get: {path}")


class TestPerExchangeRestSource(unittest.TestCase):
    def test_rest_fallback_uses_adapter_get_klines(self):
        now = int(time.time())
        rows = [
            {"t": now - (10 - i) * 900, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1.0}
            for i in range(10)
        ]
        for venue in ("binance", "okx", "bybit", "bitget", "hyperliquid", "gate"):
            client = VenueClient(venue, rows)
            res = resolve_candles(
                client, "BTC_USDT", "15m", 5,
                MarketConfig(mode="rest_only", exchange=venue),
                env="live",
            )
            self.assertEqual(res.source, "exchange", venue)
            self.assertEqual(client.kline_calls, 1, venue)
            self.assertEqual(client.public_calls, 0, venue)
            self.assertEqual(len(res.rows), 5, venue)
            self.assertEqual(res.rows[-1]["c"], 1.5)

    def test_hybrid_prefers_matching_local_db_then_venue_rest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            now = int(time.time())
            data = tmp / "data"
            data.mkdir()
            # binance-only local db
            db = data / "kline_binance.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE kline (t INTEGER, symbol TEXT, interval TEXT, o REAL,"
                " h REAL, l REAL, c REAL, v REAL, sum REAL, ema20 REAL, atr14 REAL)"
            )
            for i in range(20):
                conn.execute(
                    "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (now - (20 - i) * 900, "BTC_USDT", "15m", 1, 2, 0.5, 1.5, 1, 0, 1.4, 0.2),
                )
            conn.commit()
            conn.close()

            # gate venue must NOT see binance local db; falls to its own REST
            gate_rows = [
                {"t": now - (5 - i) * 900, "o": 9.0, "h": 9.5, "l": 8.5, "c": 9.1, "v": 1.0}
                for i in range(5)
            ]
            gate_c = VenueClient("gate", gate_rows)
            res_gate = resolve_candles(
                gate_c, "BTC_USDT", "15m", 5,
                MarketConfig(mode="hybrid", exchange="gate", pa_data_root=str(data)),
                env="live",
            )
            self.assertEqual(res_gate.source, "exchange")
            self.assertEqual(gate_c.kline_calls, 1)
            self.assertEqual(res_gate.rows[-1]["c"], 9.1)

            # binance venue uses its own local db
            bin_c = VenueClient("binance", gate_rows)
            res_bin = resolve_candles(
                bin_c, "BTC_USDT", "15m", 5,
                MarketConfig(mode="hybrid", exchange="binance", pa_data_root=str(data)),
                env="live",
            )
            self.assertEqual(res_bin.source, "local")
            self.assertEqual(bin_c.kline_calls, 0)
            self.assertEqual(res_bin.rows[-1]["c"], 1.5)

    def test_db_name_follows_market_exchange(self):
        root = Path("/tmp/pa")
        cases = {
            "gate": "kline.db",
            "binance": "kline_binance.db",
            "okx": "kline_okx.db",
            "bybit": "kline_bybit.db",
            "bitget": "kline_bitget.db",
            "hyperliquid": "kline_hyperliquid.db",
        }
        for ex, name in cases.items():
            p = resolve_db_path("live", MarketConfig(mode="hybrid", exchange=ex), root)
            self.assertEqual(p.name, name, ex)


if __name__ == "__main__":
    unittest.main()
