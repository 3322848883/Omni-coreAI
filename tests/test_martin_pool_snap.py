# -*- coding: utf-8 -*-
"""马丁池两段式 + 补点间距动态化（2026-11-03）。

用户口径：
1. 「马丁三层放在 1h/4h/1d SMC 结构位，网格穿插在 5m/15m/1h」——
   旧槽位制（第 1/5/10 近发马丁张数）在 AI 浅密位排布下无法吸附深池位
   （替换会破坏单调序）。改为**马丁先定、网格穿插**：马丁 3 档从
   `mtf_martin_intervals`（1h/4h/1d）池按深度窗口（martin_windows）各取
   「深度最小」位；网格 7 档从 5m/15m/1h 池由近到远穿插。
2. 「补档也需要动态不要写死15点」——min_gap 改物理下限后 ext_gap 曾被
   钉死 15 点；现跟随当轮档距（≥ step×0.8），被 [15, 50] 点硬夹。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402

MARK = 2332.71


def _candles(lo: float, hi: float, n: int = 40):
    """平坦 K 线：区间极值兜底（min low / max high）必入池。"""
    return [{"t": 1793000000 + i * 900, "o": (lo + hi) / 2, "h": hi,
             "l": lo, "c": (lo + hi) / 2, "v": 1.0} for i in range(n)]


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def get_last_price(self, sym):
        return MARK

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return []

    def list_price_orders(self, sym):
        return []


def _grid_levels_below(n=20, start=0.4, step=0.75):
    """近端浅位（间隔 0.75% ≈ 17.5 点 > min_gap，不会被去重）。"""
    return [round(MARK * (1 - (start + i * step) / 100.0), 2) for i in range(n)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _ex(self, tfs_rows, mtf_martin=("1h", "4h", "1d"),
            dyn_step=None, windows=None):
        c = Client()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], root=self.tmp, bot_id="mps")
        ld = {"enabled": True, "min_levels": 10, "max_levels": 10,
              "structure_only": True, "min_gap_pct": 0.15,
              "auto_extract": False}
        if mtf_martin:
            ld["mtf_martin_intervals"] = list(mtf_martin)
        if windows:
            ld["martin_windows"] = windows
        tpl = {
            "enabled": True, "grid_levels": 10, "grid_lots": 1,
            "martin_slots": {1: 3, 5: 4, 10: 6}, "martin_slots_by_index": True,
            "symbol": "ETH_USDT",
            "levels_dynamic": ld,
            "dynamic": {"enabled": False, "min_step_pct": 0.66,
                        "max_step_pct": 2.2, "max_span_pct": 25.0,
                        "min_step_points": 15, "max_step_points": 50},
        }
        ex.ladder_template = tpl
        ex._smc_rows = tfs_rows            # 注入：_engine_smc_levels 直接用
        ex._round_meta = {
            "structure_levels": _grid_levels_below(),
            "ladder_step_pct": 0.8,
        }
        cfg = dict(tpl["levels_dynamic"])
        if dyn_step is not None:
            cfg["_dyn_step_pct"] = dyn_step
        return ex, cfg

    def _targets(self, ex, cfg, side="long", mark=MARK):
        return ex._structure_targets("ETH_USDT", mark, side, cfg, {}, ex.ladder_template)


class TestFillerGapDynamic(Base):
    """补点间距动态化：跟随当轮档距，被 [15,50] 点硬夹。"""

    def _filler_gap(self, dyn_step):
        # AI 位只给 3 个（间隔够宽）→ 触发补点
        ex, cfg = self._ex({}, mtf_martin=None, dyn_step=dyn_step)
        ex._round_meta = {"structure_levels": [2316.4, 2283.6, 2250.9]}
        want, note = self._targets(ex, cfg)
        ps = [p for p, _ in want]
        gaps = [round(abs(ps[i] - ps[i + 1]), 2) for i in range(len(ps) - 1)]
        return note, gaps

    def test_gap_follows_step(self):
        """step 1.2%（27 点）→ 补点 ≈ 21.6 点，不再钉死 15。"""
        note, gaps = self._filler_gap(1.2)
        want = round(1.2 / 100 * MARK * 0.8, 1)
        self.assertGreater(min(gaps), 19.0,
                           f"补点间距应 ≈{want} 点，实际 {gaps}")
        self.assertLess(min(gaps), 24.0, f"补点间距过大：{gaps}")
        self.assertIn("filler_gap_pts", note)

    def test_gap_floor_15_points(self):
        """step 0.4%（9 点）→ 被下限夹到 15 点。"""
        _, gaps = self._filler_gap(0.4)
        for g in gaps:
            self.assertGreaterEqual(g, 14.9, f"补点 {g} 低于 15 点下限")

    def test_gap_cap_50_points(self):
        """step 3.0%（70 点）→ 被上限夹到 50 点。"""
        _, gaps = self._filler_gap(3.0)
        for g in gaps:
            self.assertLessEqual(g, 50.1, f"补点 {g} 超出 50 点上限")


class TestMartinPoolTwoStage(Base):
    """马丁先定（1h/4h/1d 池）、网格穿插（AI 浅位）。"""

    def _rows(self):
        # 池位（区间极值兜底必入池）：
        #   1h: 2245.0 → 深度 3.76%（马1 窗口 [2,4.5]）
        #   4h: 2205.0 → 5.48%（马2 [4.5,6.5]）
        #   1d: 2140.0 → 8.25%（马3 [6.5,10]）
        #   1h 高点 2290 → 1.83%（窗口外，网格区，不应成为马丁位）
        # low=high：池里只出现目标位（区间极值兜底 min/max 同价）
        return {"1h": _candles(2245.0, 2245.0),
                "4h": _candles(2205.0, 2205.0),
                "1d": _candles(2140.0, 2140.0)}

    def test_martin_slots_snap_to_deep_pool(self):
        ex, cfg = self._ex(self._rows())
        want, note = self._targets(ex, cfg)
        snaps = note.get("martin_snap") or []
        self.assertEqual(len(snaps), 3, f"三个马丁位都要有留痕：{snaps}")
        tos = sorted(s["to"] for s in snaps if "to" in s)
        self.assertIn(2245.0, tos, f"马1 应吸附 1h 池位 2245：{snaps}")
        self.assertIn(2205.0, tos, f"马2 应吸附 4h 池位 2205：{snaps}")
        self.assertIn(2140.0, tos, f"马3 应吸附 1d 池位 2140：{snaps}")

    def test_martin_lots_follow_slots(self):
        """吸附后的马丁档带 3/4/6 张（下游按张数推断 unit）。"""
        ex, cfg = self._ex(self._rows())
        want, _ = self._targets(ex, cfg)
        lots = {p: l for p, l in want}
        self.assertEqual(lots.get(2245.0), 3.0)
        self.assertEqual(lots.get(2205.0), 4.0)
        self.assertEqual(lots.get(2140.0), 6.0)

    def test_grid_levels_interleave(self):
        """网格档来自 AI 浅位（5m/15m/1h 口径），与马丁位 ≥15 点、共 10 档。"""
        ex, cfg = self._ex(self._rows())
        want, note = self._targets(ex, cfg)
        ps = [p for p, _ in want]
        self.assertEqual(len(ps), 10, f"应铺满 10 档：{ps}")
        grid = [p for p, l in want if l == 1.0]
        self.assertEqual(len(grid), 7, f"7 个网格档：{want}")
        for p in grid:
            self.assertLess(p, MARK * (1 - 0.004), "网格应在现价下方浅端")
        # 与马丁位不贴档
        for m in (2245.0, 2205.0, 2140.0):
            for g in grid:
                self.assertGreaterEqual(abs(m - g), 14.9,
                                        f"网格 {g} 与马丁 {m} 贴档")
        # 单调（多侧价格严格递减）
        self.assertEqual(ps, sorted(ps, reverse=True))

    def test_fallback_when_pool_empty(self):
        """池算不出来 → 窗口下沿兜底（2%/4.5%/6.5%），标 fallback。"""
        ex, cfg = self._ex({}, mtf_martin=("1h", "4h", "1d"))  # 无注入 → 池空
        want, note = self._targets(ex, cfg)
        snaps = note.get("martin_snap") or []
        self.assertEqual(len(snaps), 3)
        self.assertTrue(all("fallback" in s for s in snaps), f"{snaps}")
        depth = lambda p: round((MARK - p) / MARK * 100, 1)
        ps = sorted((p for p, _ in want if p not in
                     [s["fallback"] for s in snaps]), reverse=True)
        fb = sorted((s["fallback"] for s in snaps), reverse=True)
        self.assertAlmostEqual(depth(fb[0]), 2.0, places=1)
        self.assertAlmostEqual(depth(fb[1]), 4.5, places=1)
        self.assertAlmostEqual(depth(fb[2]), 6.5, places=1)
        self.assertEqual(len(fb) + len(ps), 10)

    def test_old_slot_mode_unchanged_without_martin_tfs(self):
        """未配 mtf_martin_intervals → 旧槽位制（兼容既有行为）。"""
        ex, cfg = self._ex({}, mtf_martin=None)
        want, note = self._targets(ex, cfg)
        self.assertNotIn("martin_snap", note)
        self.assertNotIn("martin_pool", note)
        lots = [l for _, l in want]
        self.assertEqual(lots[0], 3.0, "槽位制：第 1 近 = 3 张")
        self.assertEqual(lots[4], 4.0, "槽位制：第 5 近 = 4 张")
        self.assertEqual(lots[9], 6.0, "槽位制：第 10 近 = 6 张")


class TestShortSideMirror(Base):
    def test_short_side_mirrors(self):
        ex = None
        c = Client()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], root=self.tmp, bot_id="mps")
        tpl_ld = {"enabled": True, "min_levels": 10, "max_levels": 10,
                  "structure_only": True, "min_gap_pct": 0.15,
                  "auto_extract": False, "mtf_martin_intervals": ["1h", "4h", "1d"]}
        ex.ladder_template = {
            "enabled": True, "grid_levels": 10, "grid_lots": 1,
            "martin_slots": {1: 3, 5: 4, 10: 6}, "martin_slots_by_index": True,
            "symbol": "ETH_USDT", "levels_dynamic": tpl_ld,
            "dynamic": {"enabled": False, "min_step_pct": 0.66,
                        "max_step_pct": 2.2, "max_span_pct": 25.0,
                        "min_step_points": 15, "max_step_points": 50},
        }
        ex._smc_rows = {"1h": _candles(2385.0, 2385.0),    # 深度 2.24% 上方
                        "4h": _candles(2460.0, 2460.0),    # 5.48%
                        "1d": _candles(2525.0, 2525.0)}    # 8.24%
        ex._round_meta = {"structure_levels":
                          [round(MARK * (1 + (0.4 + i * 0.75) / 100), 2)
                           for i in range(20)]}
        cfg = dict(tpl_ld)
        want, note = ex._structure_targets("ETH_USDT", MARK, "short", cfg, {},
                                           ex.ladder_template)
        snaps = note.get("martin_snap") or []
        tos = sorted(s["to"] for s in snaps if "to" in s)
        self.assertIn(2385.0, tos, f"空侧马1 吸附上方 1h 位：{snaps}")
        self.assertIn(2460.0, tos)
        self.assertIn(2525.0, tos)
        ps = [p for p, _ in want]
        self.assertEqual(ps, sorted(ps), "空侧由低到高单调")


if __name__ == "__main__":
    unittest.main()


class TestDirectionFilter(Base):
    """🔴 方向过滤（2026-11-03 真实行情抓到的 bug）。

    池位两侧都含：多头吸附时若不滤方向，会把 mark **上方**的池位吸进多头 ——
    限价买单挂在现价上方 = 立即成交且方向完全错误（实测多侧马丁挂到
    2303/2355/2409 而 mark≈2252）。
    """

    def test_long_ignores_pool_above_mark(self):
        # 池位全部在 mark 上方（本属于空侧的 1h/4h/1d 位）
        ex, cfg = self._ex({"1h": _candles(2385.0, 2385.0),
                            "4h": _candles(2460.0, 2460.0),
                            "1d": _candles(2525.0, 2525.0)})
        want, note = self._targets(ex, cfg, side="long")
        snaps = note.get("martin_snap") or []
        self.assertTrue(all("fallback" in s for s in snaps),
                        f"多侧不得吸附 mark 上方的池位：{snaps}")
        for p, _ in want:
            self.assertLess(p, MARK, f"多侧档位 {p} 不得高于现价")
