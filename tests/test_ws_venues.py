import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pa-data-source"))
sys.path.insert(0, str(ROOT))

from ws_venues import (  # noqa: E402
    BinanceWS,
    BitgetWS,
    BybitWS,
    HyperliquidWS,
    OkxWS,
    VENUES,
    get_venue,
)


class TestVenueSubscriptions(unittest.TestCase):
    def test_all_five_registered(self):
        for name in ("binance", "okx", "bybit", "bitget", "hyperliquid"):
            self.assertIn(name, VENUES)
            self.assertTrue(get_venue(name).url.startswith("wss://"))

    def test_subscribe_payloads(self):
        h = HyperliquidWS().subscribe("BTC_USDT", "15m")
        self.assertEqual(h[0]["subscription"]["type"], "candle")
        self.assertEqual(h[0]["subscription"]["coin"], "BTC")
        self.assertEqual(h[0]["subscription"]["interval"], "15m")

        b = BinanceWS().subscribe("BTC_USDT", "15m")
        self.assertIn("btcusdt@kline_15m", b[0]["params"][0])

        o = OkxWS().subscribe("BTC_USDT", "1h")
        self.assertEqual(o[0]["args"][0]["instId"], "BTC-USDT-SWAP")
        self.assertEqual(o[0]["args"][0]["channel"], "candle1H")

        y = BybitWS().subscribe("BTC_USDT", "15m")
        self.assertIn("kline.15.BTCUSDT", y[0]["args"][0])

        g = BitgetWS().subscribe("BTC_USDT", "1h")
        self.assertEqual(g[0]["arg"]["channel"], "candle1H")
        self.assertEqual(g[0]["arg"]["instId"], "BTCUSDT")


class TestVenueParseGateShaped(unittest.TestCase):
    """WS 解出的 bar 必须是 Gate 形状：t,o,h,l,c,v,sum（ema 由库层算）。"""

    def test_hyperliquid_candle(self):
        msg = json.dumps({
            "channel": "candle",
            "data": {"t": 1790437200000, "T": 1790437259999, "s": "BTC", "i": "1m",
                     "o": "84110.0", "c": "84109.0", "h": "84110.0", "l": "84109.0",
                     "v": "0.027", "n": 22},
        })
        rows = HyperliquidWS().parse(msg)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["t"], 1790437200)
        self.assertEqual(r["c"], 84109.0)
        self.assertEqual(r["symbol"], "BTC_USDT")
        self.assertEqual(r["interval"], "1m")
        for k in ("o", "h", "l", "v", "sum"):
            self.assertIn(k, r)

    def test_hyperliquid_ignores_sub_ack(self):
        msg = json.dumps({"channel": "subscriptionResponse", "data": {"method": "subscribe"}})
        self.assertEqual(HyperliquidWS().parse(msg), [])

    def test_binance_kline(self):
        msg = json.dumps({
            "stream": "btcusdt@kline_15m",
            "data": {"e": "kline", "E": 1, "s": "BTCUSDT", "k": {
                "t": 1790434800000, "o": "1", "h": "2", "l": "0.5", "c": "1.5",
                "v": "10", "q": "15", "i": "15m",
            }},
        })
        rows = BinanceWS().parse(msg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["t"], 1790434800)
        self.assertEqual(rows[0]["symbol"], "BTC_USDT")
        self.assertEqual(rows[0]["sum"], 15.0)

    def test_okx_candle(self):
        msg = json.dumps({
            "arg": {"channel": "candle15m", "instId": "BTC-USDT-SWAP"},
            "data": [["1790434800000", "1", "2", "0.5", "1.5", "10", "100", "150", "1"]],
        })
        rows = OkxWS().parse(msg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["t"], 1790434800)
        self.assertEqual(rows[0]["symbol"], "BTC_USDT")
        self.assertEqual(rows[0]["v"], 10.0)

    def test_bybit_kline(self):
        msg = json.dumps({
            "topic": "kline.15.BTCUSDT",
            "data": [{"start": 1790434800000, "open": "1", "high": "2", "low": "0.5",
                      "close": "1.5", "volume": "10", "turnover": "15", "symbol": "BTCUSDT"}],
        })
        rows = BybitWS().parse(msg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["t"], 1790434800)
        self.assertEqual(rows[0]["symbol"], "BTC_USDT")

    def test_bitget_candle(self):
        msg = json.dumps({
            "action": "snapshot",
            "arg": {"instType": "USDT-FUTURES", "channel": "candle15m", "instId": "BTCUSDT"},
            "data": [["1790434800000", "1", "2", "0.5", "1.5", "10"]],
        })
        rows = BitgetWS().parse(msg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["t"], 1790434800)
        self.assertEqual(rows[0]["symbol"], "BTC_USDT")

    def test_parse_garbage_safe(self):
        for v in VENUES.values():
            self.assertEqual(v.parse("not json"), [])
            self.assertEqual(v.parse(json.dumps({"foo": 1})), [])
            self.assertEqual(v.parse(json.dumps([1, 2, 3])), [])


if __name__ == "__main__":
    unittest.main()
