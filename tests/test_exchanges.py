import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.exchanges import create_exchange, list_exchanges
from gate_bot.exchanges.base import ExchangeClient
from gate_bot.exchanges.gate import GateExchange
from gate_bot.exchanges.binance import BinanceExchange
from gate_bot.exchanges.okx import OkxExchange, OkxMapper
from gate_bot.exchanges.bybit import BybitExchange
from gate_bot.exchanges.bitget import BitgetExchange, BitgetMapper
from gate_bot.exchanges.hyperliquid import HyperliquidExchange, HyperliquidMapper


class TestExchangeFactory(unittest.TestCase):
    def test_six_exchanges_registered(self):
        names = set(list_exchanges())
        for n in ("gate", "binance", "okx", "bybit", "bitget", "hyperliquid"):
            self.assertIn(n, names)

    def test_create_all(self):
        for n in list_exchanges():
            c = create_exchange(n, env="testnet", api_key="k", api_secret="s")
            self.assertIsInstance(c, ExchangeClient)
            self.assertEqual(c.name, n)


class TestSymbolMappers(unittest.TestCase):
    def test_binance_strip(self):
        c = BinanceExchange()
        self.assertEqual(c.mapper.native("BTC_USDT"), "BTCUSDT")
        self.assertEqual(c.mapper.internal("BTCUSDT"), "BTC_USDT")

    def test_okx(self):
        m = OkxMapper()
        self.assertEqual(m.native("BTC_USDT"), "BTC-USDT-SWAP")
        self.assertEqual(m.internal("BTC-USDT-SWAP"), "BTC_USDT")

    def test_hyperliquid(self):
        m = HyperliquidMapper()
        self.assertEqual(m.native("BTC_USDT"), "BTC")
        self.assertEqual(m.internal("BTC"), "BTC_USDT")

    def test_bitget(self):
        m = BitgetMapper()
        self.assertEqual(m.native("ETH_USDT"), "ETHUSDT")
        self.assertEqual(m.internal("ETHUSDT"), "ETH_USDT")


class TestBinanceOrderBody(unittest.TestCase):
    def test_place_order_maps_fields(self):
        b = BinanceExchange(env="testnet", api_key="k", api_secret="s")
        captured = {}

        def fake(method, path, params=None, signed=False):
            captured.update(params or {})
            return {"orderId": 1}
        b._sig = fake
        b.place_order({"contract": "BTC_USDT", "size": -3, "price": 0, "tif": "IOC", "reduce_only": True})
        self.assertEqual(captured.get("symbol"), "BTCUSDT")
        self.assertEqual(captured.get("side"), "SELL")
        self.assertEqual(captured.get("quantity"), "3")
        self.assertEqual(captured.get("type"), "MARKET")


class TestGateWrap(unittest.TestCase):
    def test_gate_name(self):
        self.assertEqual(GateExchange.name, "gate")
        self.assertTrue(OkxExchange.supports_price_orders)
        self.assertTrue(BybitExchange.supports_price_orders)
        self.assertTrue(BitgetExchange.supports_price_orders)


class TestLocalDbPerExchange(unittest.TestCase):
    def test_db_path_per_exchange(self):
        from pathlib import Path
        from gate_bot.strategist.market import MarketConfig, resolve_db_path

        for ex, needle in [
            ("gate", "kline_testnet.db"),
            ("binance", "kline_binance_testnet.db"),
            ("okx", "kline_okx_testnet.db"),
        ]:
            p = resolve_db_path("testnet", MarketConfig(mode="hybrid", exchange=ex), Path("/tmp"))
            self.assertTrue(p.name.endswith(needle), p.name)

    def test_tools_use_market_cfg_exchange(self):
        from gate_bot.strategist.market import MarketConfig

        cfg = MarketConfig(mode="rest_only", exchange="binance", indicators=["ema20", "atr14"])
        self.assertEqual(cfg.exchange, "binance")
        self.assertEqual(cfg.indicators, ["ema20", "atr14"])
