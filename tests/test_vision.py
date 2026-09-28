"""K 线图视觉识别测试。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestVisionChart(unittest.TestCase):
    def _klines(self, n=50):
        return [
            {"o": 84000 + i * 20, "h": 84100 + i * 20, "l": 83900 + i * 20,
             "c": 84050 + i * 20, "v": 1000 + i * 10}
            for i in range(n)
        ]

    def test_generate_chart(self):
        from gate_bot.strategist.vision import generate_candlestick_chart
        png = generate_candlestick_chart(self._klines(), symbol="BTC_USDT", timeframe="1h")
        self.assertGreater(len(png), 1000)  # 非空 PNG
        self.assertTrue(png.startswith(b"\x89PNG"))  # PNG magic

    def test_generate_chart_alt_keys(self):
        """open/high/low/close/volume 别名也能用。"""
        from gate_bot.strategist.vision import generate_candlestick_chart
        klines = [
            {"open": 100, "high": 110, "low": 90, "close": 105, "volume": 500}
            for _ in range(30)
        ]
        png = generate_candlestick_chart(klines)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_empty_klines(self):
        from gate_bot.strategist.vision import generate_candlestick_chart
        png = generate_candlestick_chart([])
        self.assertEqual(png, b"")

    def test_base64_encode(self):
        from gate_bot.strategist.vision import generate_and_encode
        b64 = generate_and_encode(self._klines(), symbol="BTC_USDT")
        self.assertIsNotNone(b64)
        self.assertTrue(b64.startswith("data:image/png;base64,"))

    def test_output_path(self):
        import tempfile
        from gate_bot.strategist.vision import generate_candlestick_chart
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "chart.png"
            generate_candlestick_chart(self._klines(), output_path=out)
            self.assertTrue(out.exists())
            self.assertTrue(out.read_bytes().startswith(b"\x89PNG"))

    def test_large_dataset(self):
        from gate_bot.strategist.vision import generate_candlestick_chart
        png = generate_candlestick_chart(self._klines(200))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_single_candle(self):
        from gate_bot.strategist.vision import generate_candlestick_chart
        png = generate_candlestick_chart(self._klines(1))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_malformed_data_skipped(self):
        from gate_bot.strategist.vision import generate_candlestick_chart
        klines = [{"o": 0, "h": 0, "l": 0, "c": 0, "v": 0}] * 5 + self._klines(20)
        png = generate_candlestick_chart(klines)
        self.assertTrue(png.startswith(b"\x89PNG"))


class TestVisionConfig(unittest.TestCase):
    def test_vision_default_true(self):
        from gate_bot.strategist.loop import StrategistConfig
        cfg = StrategistConfig()
        self.assertTrue(cfg.vision)

    def test_vision_configurable(self):
        from gate_bot.strategist.loop import StrategistConfig
        cfg = StrategistConfig(vision=False)
        self.assertFalse(cfg.vision)

    def test_chart_none_when_disabled(self):
        """vision=False 时 _generate_chart 不应被调用。"""
        from gate_bot.strategist.loop import StrategistConfig
        self.assertFalse(StrategistConfig(vision=False).vision)


class TestVisionMessages(unittest.TestCase):
    def test_build_messages_with_chart(self):
        from gate_bot.strategist.prompt import build_messages
        system, user = build_messages(
            "test", {"a": 1}, {}, ["BTC_USDT"],
            chart_base64="data:image/png;base64,abc",
        )
        self.assertIsInstance(user, list)
        self.assertEqual(user[0]["type"], "text")
        self.assertEqual(user[1]["type"], "image_url")
        self.assertEqual(user[1]["image_url"]["url"], "data:image/png;base64,abc")

    def test_build_messages_without_chart(self):
        from gate_bot.strategist.prompt import build_messages
        system, user = build_messages("test", {"a": 1}, {}, ["BTC_USDT"])
        self.assertIsInstance(user, str)


if __name__ == "__main__":
    unittest.main()
