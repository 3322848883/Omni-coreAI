# -*- coding: utf-8 -*-
"""`indicators` / `klines` 工具必须按指标所需根数预热取数。

2026-10-27 实盘病（pa-eth-v5）：
  模型想看最近 5 根 K 线做逐 K 判读 → `limit=5`（工具 schema 的 minimum 就是 5，
  传参完全合规）；但工具把 `limit` 同时当成**取数根数**传给 `resolve_candles`
  → 只取回 5 根 → `ema20` 需要 20 期（+5 根预热 = 25）→ 算不出来返回 None
  → 模型认为「4h/1h/15m EMA20 缺失 → Gate #1 不过 → 整轮 hold」。

后果量化：当天 119 轮里 92 轮（77%）写着「缺 4h/1h/15m EMA20 → hold」；
该 bot 的 `market` 段没有 `extra_timeframes`，HTF 指标**只能靠这个工具**，
所以工具一挂，bot 就退化成「只能管仓、不能开仓」。

本文件钉住三条：
  1. `limit=5` 时取数根数必须被抬到指标预热需求（ema20 → 25）；
  2. 返回给模型的 `rows` 仍然只给 `limit` 行（token 不涨）；
  3. `ema200` 需要的 1005 根不能被 1000 上限截断（否则偏 ~2.8%）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist import tools as T  # noqa: E402
from omnialpha.strategist.market import MarketConfig  # noqa: E402
from omnialpha.strategist.indicators import indicator_warmup_bars  # noqa: E402

SYM = "ETH_USDT"


def _candles(n: int) -> list[dict]:
    """构造 n 根单调上行的 K 线（EMA 可算、ATR 可算）。"""
    out = []
    for i in range(n):
        px = 2300.0 + i * 0.5
        out.append({"t": 1793000000 + i * 300, "o": px, "h": px + 0.8,
                    "l": px - 0.8, "c": px + 0.3, "v": 100.0})
    return out


class _Stub:
    """替身：记录被请求的根数，返回恰好那么多根。"""

    def __init__(self):
        self.calls: list[int] = []

    def __call__(self, client, sym, tf, limit, **kw):
        self.calls.append(int(limit))
        return SimpleNamespace(rows=_candles(int(limit)), source="stub",
                               stale=False, age_sec=0.0, health=None)


def _run(name: str, args: dict, market_cfg=None):
    stub = _Stub()
    orig = T.resolve_candles
    T.resolve_candles = stub
    try:
        out = T.run_tool(None, name, args, env="live", bot_root=None,
                         market_cfg=market_cfg)
    finally:
        T.resolve_candles = orig
    return out, stub


class TestIndicatorsWarmup(unittest.TestCase):
    def test_limit_5_still_computes_ema20(self):
        """limit=5（模型只想看 5 行）→ 取数必须抬到 25 根，ema20 不能是 None。"""
        out, stub = _run("indicators", {"symbol": SYM, "tf": "4h",
                                        "names": ["ema20"], "limit": 5})
        self.assertEqual(stub.calls, [indicator_warmup_bars(["ema20"])],
                         f"只取了 {stub.calls} 根，应抬到预热需求")
        self.assertIsNotNone(out["latest"].get("ema20"),
                             f"ema20 仍为 None: {out['latest']}")
        self.assertEqual(len(out["rows"]), 5, "返回给模型的行数仍应只有 limit 行")

    def test_multiple_names_take_max_warmup(self):
        out, stub = _run("indicators", {"symbol": SYM, "tf": "1h",
                                        "names": ["ema20", "atr14"], "limit": 5})
        self.assertEqual(stub.calls, [indicator_warmup_bars(["ema20", "atr14"])])
        self.assertIsNotNone(out["latest"].get("ema20"))

    def test_ema200_not_capped_at_1000(self):
        """ema200 需要 1005 根（含预热），上限不能截成 1000。"""
        out, stub = _run("indicators", {"symbol": SYM, "tf": "4h",
                                        "names": ["ema200"], "limit": 5})
        need = indicator_warmup_bars(["ema200"])
        self.assertGreater(need, 1000, "假设 ema200 预热 >1000 根")
        self.assertEqual(stub.calls, [need], f"被上限截断了: {stub.calls} vs {need}")
        self.assertIsNotNone(out["latest"].get("ema200"))

    def test_large_limit_not_shrunk(self):
        """模型要 200 行 → 取数不少于 200（warmup 更小的时候不能反缩）。"""
        out, stub = _run("indicators", {"symbol": SYM, "tf": "5m",
                                        "names": ["ema20"], "limit": 200})
        self.assertEqual(stub.calls, [200])
        self.assertEqual(len(out["rows"]), 200)


class TestKlinesCarriedIndicators(unittest.TestCase):
    """klines 分支同样带指标（market_cfg.indicators），也必须预热取数。"""

    def test_klines_limit_5_with_ema20(self):
        cfg = MarketConfig(mode="rest_only", indicators=["ema20", "atr14"])
        out, stub = _run("klines", {"symbol": SYM, "tf": "15m", "limit": 5}, cfg)
        self.assertEqual(stub.calls, [indicator_warmup_bars(["ema20", "atr14"])])
        self.assertEqual(len(out["rows"]), 5, "返回行数仍按 limit 裁")
        last = out["rows"][-1]
        self.assertIsNotNone(last.get("ema20"), f"行内 ema20 为空: {last}")

    def test_klines_without_indicators_uses_limit(self):
        cfg = MarketConfig(mode="rest_only", indicators=[])
        out, stub = _run("klines", {"symbol": SYM, "tf": "15m", "limit": 12}, cfg)
        self.assertEqual(stub.calls, [12], "没有指标需求时不该多取")


if __name__ == "__main__":
    unittest.main()
