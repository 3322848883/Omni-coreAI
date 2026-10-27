# -*- coding: utf-8 -*-
"""写入端指标引擎（pa-data-source/indicators_core.py）与读取端实现的 A/B 对拍。

两层保证：
1. **批量函数逐位一致**：新引擎 vs `omnialpha.strategist.indicators`（同一公式，误差 ≤1e-9）；
2. **流式 == 批量逐位相等**：`StreamState` 逐根喂入的结果与批量计算结果 `==`（bit 相同），
   这样 WS 收线路径可以走增量，而回填/重算走批量，两条路径不会产生口径分叉。
"""
import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

import indicators_core as core  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402
from omnialpha.strategist.indicators import attach_indicators  # noqa: E402

NAMES = ["ema20", "ema50", "ema200", "atr14", "rsi14", "macd", "macd_dea", "macd_hist", "boll20"]


def make_bars(n=1200, seed=7):
    rnd = random.Random(seed)
    price = 2400.0
    rows = []
    for i in range(n):
        o = price
        c = max(1.0, o + rnd.gauss(0, 1.6))
        h = max(o, c) + abs(rnd.gauss(0, 0.8))
        l = min(o, c) - abs(rnd.gauss(0, 0.8))
        rows.append({"t": 1_700_000_000 + i * 3600, "o": o, "h": h, "l": l, "c": c,
                     "v": 1000.0 + rnd.random() * 100, "sum": 0.0})
        price = c
    return rows


def close_enough(a, b, rel=1e-9, abs_=1e-9):
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_)


class TestBatchMatchesReadSide(unittest.TestCase):
    """批量函数必须与读取端 attach_indicators 的输出一致。"""

    @classmethod
    def setUpClass(cls):
        cls.bars = make_bars()
        cls.ref = attach_indicators([dict(r) for r in cls.bars], NAMES)
        cls.h = [r["h"] for r in cls.bars]
        cls.l = [r["l"] for r in cls.bars]
        cls.c = [r["c"] for r in cls.bars]

    def _check(self, ours, key):
        for i, (a, b) in enumerate(zip(ours, [r.get(key) for r in self.ref])):
            self.assertTrue(close_enough(a, b),
                            f"{key}[{i}] ours={a} ref={b}")

    def test_ema(self):
        for p in (20, 50, 200):
            self._check(core.ema(self.c, p), f"ema{p}")

    def test_atr(self):
        self._check(core.atr(self.h, self.l, self.c, 14), "atr14")

    def test_rsi(self):
        self._check(core.rsi(self.c, 14), "rsi14")

    def test_macd(self):
        series = core.macd(self.c, 12, 26, 9)
        self._check(series["dif"], "macd")
        self._check(series["dea"], "macd_dea")
        self._check(series["hist"], "macd_hist")

    def test_boll(self):
        series = core.boll(self.c, 20, 2.0)
        self._check(series["middle"], "boll20")
        self._check(series["upper"], "boll_upper")
        self._check(series["lower"], "boll_lower")


class TestStreamMatchesBatch(unittest.TestCase):
    """流式增量必须与批量逐位相等（同一序列、同一参数）。"""

    def test_stream_bitexact(self):
        bars = make_bars(1200, seed=11)
        specs = [s.as_dict() for s in parse_indicators(None)]
        st = core.StreamState(specs)
        streamed = [st.push(r["o"], r["h"], r["l"], r["c"]) for r in bars]

        h = [r["h"] for r in bars]
        l = [r["l"] for r in bars]
        c = [r["c"] for r in bars]
        batch = {
            "ema20": core.ema(c, 20), "ema50": core.ema(c, 50), "ema200": core.ema(c, 200),
            "atr14": core.atr(h, l, c, 14), "rsi14": core.rsi(c, 14),
        }
        macd = core.macd(c, 12, 26, 9)
        batch.update({"macd": macd["dif"], "macd_dea": macd["dea"], "macd_hist": macd["hist"]})
        boll = core.boll(c, 20, 2.0)
        batch.update({"boll20": boll["middle"], "boll20_upper": boll["upper"],
                      "boll20_lower": boll["lower"]})

        for col, series in batch.items():
            for i in range(len(bars)):
                self.assertEqual(streamed[i][col], series[i],
                                 f"{col}[{i}] stream={streamed[i][col]} batch={series[i]}")

    def test_stream_warmup_none(self):
        bars = make_bars(40, seed=3)
        st = core.StreamState([{"kind": "ema", "params": [20], "columns": ["ema20"]}])
        vals = [st.push(r["o"], r["h"], r["l"], r["c"])["ema20"] for r in bars]
        self.assertTrue(all(v is None for v in vals[:19]))
        self.assertIsNotNone(vals[19])


if __name__ == "__main__":
    unittest.main()
