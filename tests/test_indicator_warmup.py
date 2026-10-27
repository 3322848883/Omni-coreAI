# -*- coding: utf-8 -*-
"""非主周期的指标预热根数（2026-10-27 实测缺陷）。

现象：ladder-v5 人格用 4H EMA200 定方向，但快照里 `tf["4h"]["indicators"]["ema200"]`
是 None，模型只能退化成 EMA20。

根因：`market.mode: rest_only` 每轮现算指标，而非主周期只拉 `extra_candles`（默认 60）
根 K 线 —— EMA200 需要 200 根，永远算不出来。本地复现（同一份代码，只改根数）：
    4h × 60  → ema200=None
    4h × 260 → ema200=2500.55
    15m × 200（主周期）→ ema200=2326.14   ← 主周期刚好够，所以"本地看起来是好的"

修法：算指标时拉 `max(extra_candles, 指标预热根数)`，**prompt 里仍只保留 extra_candles 根**。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.strategist import snapshot as snap
from omnialpha.strategist.indicators import indicator_warmup_bars
from omnialpha.strategist.market import CandleResult, MarketConfig


class TestWarmupBars(unittest.TestCase):
    def test_ema200_needs_full_convergence(self):
        """长周期 EMA 用 SMA 种子，需 ~5×周期才收敛（205 根算出的值差 2.75%）。"""
        self.assertGreaterEqual(indicator_warmup_bars(["ema200"]), 1000)

    def test_small_periods(self):
        """短周期保持原行为（atr14 → 19 根），不动 martin-grid 的取数量。"""
        self.assertEqual(indicator_warmup_bars(["atr14"]), 19)

    def test_takes_max_of_list(self):
        self.assertEqual(indicator_warmup_bars(["atr14", "ema200"]),
                         indicator_warmup_bars(["ema200"]))

    def test_empty_is_zero(self):
        self.assertEqual(indicator_warmup_bars([]), 0)

    def test_unknown_name_ignored(self):
        self.assertGreaterEqual(indicator_warmup_bars(["ema50", "no_such_ind"]), 50)


class _Recorder:
    """记录每次 resolve_candles 的 (interval, limit)。"""

    def __init__(self, rows=260):
        self.calls = []
        self.rows = rows

    def __call__(self, client, sym, tf, limit, **kw):
        self.calls.append((tf, limit))
        n = min(limit, self.rows)
        return CandleResult(
            rows=[{"t": i, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0}
                  for i in range(n)],
            source="exchange", stale=False, degraded=[],
        )


class _FakeClient:
    def get_ticker(self, sym):
        return {"last": "2400"}

    def get_contract(self, sym):
        return SimpleNamespace(quanto_multiplier=0.01, order_size_round=1,
                               order_price_round=0.1, leverage_max=50)

    def get_account(self):
        return {"available": "100", "total": "100", "position_mode": "dual"}

    def get_positions(self):
        return []

    def list_orders(self, contract=None):
        return []

    def list_price_orders(self, contract=None):
        return []


class TestSnapshotExtraTfFetch(unittest.TestCase):
    def _run(self, indicators, extra_candles=60, extra_timeframes=("4h",)):
        rec = _Recorder()
        orig = snap.resolve_candles
        snap.resolve_candles = rec
        try:
            cfg = MarketConfig(mode="rest_only", indicators=list(indicators),
                               extra_timeframes=list(extra_timeframes),
                               extra_candles=extra_candles, refresh=["candles"])
            out = snap.collect_snapshot(
                _FakeClient(), ["ETH_USDT"], candles=200, interval="15m",
                market_cfg=cfg, env="live",
            )
        finally:
            snap.resolve_candles = orig
        return rec, out

    def test_extra_tf_fetches_enough_for_ema200(self):
        """★核心回归★ 4h 必须拉到 ≥200 根才可能算出 ema200。"""
        rec, _ = self._run(["atr14", "ema200"])
        limits = [lim for tf, lim in rec.calls if tf == "4h"]
        self.assertTrue(limits, rec.calls)
        self.assertGreaterEqual(max(limits), 200, rec.calls)
        self.assertLessEqual(max(limits), 1000, "Gate candlesticks 上限 1000 根")

    def test_prompt_rows_still_trimmed(self):
        """拉得多、但快照里只留 extra_candles 根（不许 token 暴涨）。"""
        _, out = self._run(["ema200"], extra_candles=60)
        self.assertEqual(len(out["market"]["ETH_USDT"]["tf"]["4h"]["candles"]), 60)

    def test_no_ema200_keeps_small_fetch(self):
        """只要 atr14 时不该无谓多拉（保持原行为）。"""
        rec, _ = self._run(["atr14"], extra_candles=60)
        self.assertEqual([lim for tf, lim in rec.calls if tf == "4h"], [60])


if __name__ == "__main__":
    unittest.main()
