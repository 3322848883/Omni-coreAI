# -*- coding: utf-8 -*-
"""底仓 SMC 结构吸附单测：_base_pos_structure_snap 的波段/最近/兜底逻辑。

用 stub 替换 compute_smc_map（只测吸附逻辑本身，不依赖 smc_map 内部语义）；
真实端到端行为由 _test_bp_structure_snap.py（真数据多窗口）覆盖。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.strategist.smc_map import BULL, BEAR  # noqa: E402


def _stub(ob_bull=(), ob_bear=(), fvg_bull=(), fvg_bear=(),
          pivots_lo=(), pivots_hi=(), eq_lo=(), eq_hi=()):
    """构造假 SMCMapResult。ob=(low,), fvg=(bottom,), pivot=(price,)。"""
    return SimpleNamespace(
        swing_order_blocks=[SimpleNamespace(bar_low=l, bar_high=l + 6, bias=BULL)
                            for l in ob_bull],
        internal_order_blocks=[SimpleNamespace(bar_low=l, bar_high=l + 4, bias=BULL)
                               for l in ()],
        fvgs=([SimpleNamespace(top=b + 8, bottom=b, bias=BULL) for b in fvg_bull]
              + [SimpleNamespace(top=t, bottom=t - 8, bias=BEAR) for t in fvg_bear]),
        swing_pivots=([{"kind": "low", "price": p} for p in pivots_lo]
                      + [{"kind": "high", "price": p} for p in pivots_hi]),
        equal_lows=[{"price": p} for p in eq_lo],
        equal_highs=[{"price": p} for p in eq_hi],
        # 空头用的 bear OB（bar_high 为触发参考）
        **{"_bear_obs": [SimpleNamespace(bar_high=h, bar_low=h - 6, bias=BEAR)
                         for h in ob_bear]})


def _stub_mixed(stub):
    """把 bear OB 合并进 swing_order_blocks（真实返回里同池）。"""
    stub.swing_order_blocks = list(stub.swing_order_blocks) + list(
        getattr(stub, "_bear_obs", []))
    return stub


class TestBasePosStructureSnap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "kline.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS kline (t INTEGER, o REAL, h REAL,"
                    " l REAL, c REAL, v REAL, symbol TEXT, interval TEXT, closed INTEGER)")
        con.executemany(
            "INSERT INTO kline (t,o,h,l,c,v,symbol,interval,closed) VALUES (?,?,?,?,?,?,?,?,1)",
            [(i, 2400, 2401, 2399, 2400, 10, "ETH_USDT", "15m") for i in range(200)])
        con.commit()
        con.close()
        self.ex = Executor.__new__(Executor)
        self.ex.ladder_template = {"levels_dynamic": {"db_path": str(self.db)}}
        self.cfg = {"interval": "15m", "bars": 300, "tol_pct": 0.5}

    def _snap(self, side, px, stub, mark=2430.0):
        with patch("omnialpha.strategist.smc_map.compute_smc_map",
                   return_value=_stub_mixed(stub)):
            return self.ex._base_pos_structure_snap(
                "ETH_USDT", side, px, self.cfg, mark)

    # ── 多头：向上吸附，取带内最近（min）────────────────────────────────
    def test_long_snaps_to_nearest_above(self):
        stub = _stub(ob_bull=(2404.0,), fvg_bull=(2408.0,), pivots_lo=(2402.0,))
        px, note = self._snap("long", 2400.0, stub)
        self.assertTrue(note["snapped"], note)
        self.assertEqual(px, 2402.0)          # 三个候选里离 wick 最近
        self.assertEqual(note["candidates_in_band"], 3)
        self.assertGreater(note["shift_pct"], 0)

    def test_long_out_of_band_keeps_raw(self):
        """候选全在 tol 外（太高）→ 不吸附，保留裸极值。"""
        stub = _stub(ob_bull=(2450.0,), pivots_lo=(2440.0,))
        px, note = self._snap("long", 2400.0, stub)
        self.assertFalse(note["snapped"])
        self.assertEqual(px, 2400.0)

    def test_long_below_wick_ignored(self):
        """结构在 wick 下方（更深处）→ 不吸（吸附只朝成交更容易的方向）。"""
        stub = _stub(ob_bull=(2390.0,), pivots_lo=(2395.0,))
        px, note = self._snap("long", 2400.0, stub)
        self.assertFalse(note["snapped"])
        self.assertEqual(px, 2400.0)

    def test_long_bear_structures_ignored(self):
        """多头吸附只看 bull 结构 —— bear OB/FVG 在带内也不吸。"""
        stub = _stub(ob_bear=(2405.0,), fvg_bear=(2407.0,))
        px, note = self._snap("long", 2400.0, stub)
        self.assertFalse(note["snapped"])
        self.assertEqual(px, 2400.0)

    # ── 空头：向下吸附，取带内最近（max）────────────────────────────────
    def test_short_snaps_to_nearest_below(self):
        stub = _stub(ob_bear=(2596.0,), fvg_bear=(2592.0,), pivots_hi=(2590.0,))
        px, note = self._snap("short", 2600.0, stub)
        self.assertTrue(note["snapped"], note)
        self.assertEqual(px, 2596.0)          # 离 wick 最近的下方结构
        self.assertEqual(note["candidates_in_band"], 3)
        self.assertLess(note["shift_pct"], 0)

    # ── 兜底 ────────────────────────────────────────────────────────────
    def test_db_missing_failsafe(self):
        """db 不可用 → error note，原价返回（绝不阻断底仓）。"""
        ex = Executor.__new__(Executor)
        ex.ladder_template = {"levels_dynamic": {"db_path": str(
            Path(self.tmp) / "nope.db")}}
        px, note = ex._base_pos_structure_snap(
            "ETH_USDT", "long", 2400.0, self.cfg, 2430.0)
        self.assertEqual(px, 2400.0)
        self.assertIn("error", note)

    def test_equal_lows_candidate(self):
        stub = _stub(eq_lo=(2403.0,))
        px, note = self._snap("long", 2400.0, stub)
        self.assertTrue(note["snapped"])
        self.assertEqual(px, 2403.0)


if __name__ == "__main__":
    unittest.main()
