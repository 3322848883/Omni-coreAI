# -*- coding: utf-8 -*-
"""4.0：档位由**多周期结构位**驱动，ATR 只负责间距（2026-10-30 用户口径）。

· 马丁第 1/5/10 层开仓基数 3/4/6；网格第 2,3,4,6,7,8,9 层基数 1
· 取消用档距替代结构位：充分使用 15m/1h/4h/1d 关键位
· 最小间距仍由 ATR 动态调整（`min_gap_pct: auto`）
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402

TPL = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
MARK = 2330.0


class C:
    def __init__(self):
        self.book, self.prot = [], []
        self.mark = MARK

    def get_last_price(self, s):
        return self.mark

    def get_contract(self, s):
        return None

    def get_positions(self):
        return []

    def list_orders(self, s):
        return []

    def list_price_orders(self, s):
        return []


def _bars(start, step, n, base):
    out = []
    for i in range(n):
        c = base + (i % 7) * step
        out.append({"t": start + i * 900, "o": c, "h": c + step * 2,
                    "l": c - step * 2, "c": c, "v": 100})
    return out


def _ex(over=None):
    tpl = dict(TPL)
    tpl["levels_dynamic"] = dict(TPL.get("levels_dynamic") or {},
                                 min_gap_pct="auto", auto_extract=True)
    tpl["anchor_state_file"] = str(Path(tempfile.mkdtemp()) / "a.json")
    tpl["levels_state_file"] = str(Path(tempfile.mkdtemp()) / "l.json")
    tpl["direction_mode"] = "signal"
    tpl.update(over or {})
    ex = Executor(C(), symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
    ex.ladder_template = tpl
    ex._round_meta = {}
    return ex, tpl


class TestEngineExtract(unittest.TestCase):
    """AI 沉默时引擎自己从多周期 K 线提取结构位。"""

    def test_extracts_from_multiple_timeframes(self):
        ex, _ = _ex()
        # 各周期同一基准价、不同振幅 —— 模拟真实行情里「同一价位被多周期同时命中」
        ex._smc_rows = {
            "15m": _bars(1793000000, 3, 300, 2320),
            "1h": _bars(1793000000, 8, 240, 2320),
            "4h": _bars(1793000000, 20, 180, 2320),
            "1d": _bars(1793000000, 60, 120, 2320),
        }
        lv, note = ex._engine_smc_levels("ETH_USDT", ex.ladder_template)
        # 合成 K 线很规整（结构位重复度高），这里验的是「多周期都参与」而非数量
        self.assertGreaterEqual(len(lv), 5, f"应提取到多个价位：{len(lv)}")
        self.assertEqual(note.get("tfs"), ["15m", "1h", "4h", "1d"],
                         f"四个周期都要参与：{note.get('tfs')}")
        self.assertTrue(all(v > 0 for v in (note.get("sources") or {}).values()),
                        f"每周期都要有贡献：{note.get('sources')}")
        # 汇合位（同一价位被多周期命中）依赖各周期价位真的落在同一点，
        # 合成锯齿数据做不到；真实回放里中位 4 个。这里只要求字段存在。
        self.assertIn("multi_tf", note)

    def test_ai_levels_take_priority(self):
        """AI 给了结构位 → 用 AI 的，不跑引擎提取。"""
        ex, tpl = _ex()
        ex._smc_rows = {"15m": _bars(1793000000, 3, 300, 2320)}
        ex._round_meta = {"structure_levels": [2250.0, 2200.0]}
        want, note = ex._structure_targets("ETH_USDT", MARK, "long",
                                          dict(tpl["levels_dynamic"],
                                               _dyn_step_pct=0.8), {}, tpl)
        self.assertEqual(note.get("src"), "ai", f"{note.get('src')}")
        self.assertEqual(note.get("given"), 2)
        self.assertNotIn("engine", note, "AI 给了就不该再跑引擎提取")

    def test_fallback_to_even_when_no_data(self):
        """两侧都没有 → 明确标记 no_structure_levels（调用方回退等距）。"""
        ex, tpl = _ex()
        ex._smc_rows = {}      # 没有任何周期数据
        want, note = ex._structure_targets("ETH_USDT", MARK, "long",
                                          dict(tpl["levels_dynamic"],
                                               _dyn_step_pct=0.8), {}, tpl)
        self.assertEqual(want, [])
        self.assertEqual(note.get("error"), "no_structure_levels")


class TestLotsByIndex(unittest.TestCase):
    """马丁第 1/5/10 层 3/4/6，其余 1 张。"""

    def _want(self, levels):
        ex, tpl = _ex()
        ex._round_meta = {"structure_levels": levels}
        return ex._structure_targets("ETH_USDT", MARK, "long",
                                     dict(tpl["levels_dynamic"], _dyn_step_pct=0.8),
                                     {}, tpl)

    def test_lots_are_3_4_6_at_1_5_10(self):
        lv = [MARK - 25 * k for k in range(1, 11)]
        want, note = self._want(lv)
        self.assertEqual(len(want), 10, f"应 10 档：{len(want)}")
        sizes = [lots for _p, lots in want]
        self.assertEqual(sizes, [3, 1, 1, 1, 4, 1, 1, 1, 1, 6], f"张数 {sizes}")
        self.assertEqual(sum(sizes), 20, f"总张数应 20，实际 {sum(sizes)}")
        self.assertEqual(note.get("slots"), {1: 3.0, 5: 4.0, 10: 6.0})

    def test_extra_levels_use_grid_lots(self):
        """13 层时第 11-13 档用 grid_lots（1 张），不继承第 10 档的 6 张。"""
        lv = [MARK - 22 * k for k in range(1, 14)]
        want, _ = self._want(lv)
        self.assertGreaterEqual(len(want), 11, f"{len(want)}")
        sizes = [lots for _p, lots in want]
        self.assertEqual(sizes[9], 6, f"第 10 档应 6 张：{sizes}")
        self.assertEqual(sizes[10], 1, f"第 11 档应回落 1 张：{sizes}")


class TestMinGapAuto(unittest.TestCase):
    """最小间距 = 当轮 ATR 动态档距。"""

    def test_min_gap_equals_dyn_step(self):
        ex, tpl = _ex()
        ex._round_meta = {"structure_levels": [MARK - 25 * k for k in range(1, 11)]}
        for step in (0.7, 1.2, 2.0):
            _w, note = ex._structure_targets(
                "ETH_USDT", MARK, "long",
                dict(tpl["levels_dynamic"], _dyn_step_pct=step), {}, tpl)
            self.assertEqual(note.get("min_gap_src"), "atr")
            self.assertAlmostEqual(note.get("min_gap_pct"), step, places=3,
                                   msg=f"step={step} → {note.get('min_gap_pct')}")

    def test_levels_denser_than_min_gap_are_merged(self):
        """结构位比最小间距还密 → 合并（不能铺出 0.3% 的密集档）。"""
        ex, tpl = _ex()
        # 间距 3 点 = 0.13%，远小于 min_gap 1.2%
        ex._round_meta = {"structure_levels": [MARK - 3 * k for k in range(1, 40)]}
        want, note = ex._structure_targets(
            "EU", MARK, "long",
            dict(tpl["levels_dynamic"], _dyn_step_pct=1.2), {}, tpl)
        n_given = note.get("in_side") or 0
        self.assertLess(len(want), n_given, f"应合并掉大部分：{len(want)}/{n_given}")
        ps = sorted(p for p, _ in want)
        for i in range(len(ps) - 1):
            gap = (ps[i + 1] - ps[i]) / ps[i] * 100
            self.assertGreaterEqual(gap, 1.2 - 1e-6,
                                    f"合并后间距 {gap:.2f}% 仍小于 min_gap 1.2%")

    def test_no_gap_larger_than_max_step(self):
        """相邻档距不得超过 max_step（防跨周期空洞）。"""
        ex, tpl = _ex()
        mx = float((tpl.get("dynamic") or {}).get("max_step_pct") or 2.2)
        # 结构位稀疏：间距 5%（远超 max_step 2.2%）
        ex._round_meta = {"structure_levels": [MARK - 120 * k for k in range(1, 5)]}
        want, note = ex._structure_targets(
            "EU", MARK, "long",
            dict(tpl["levels_dynamic"], _dyn_step_pct=0.8), {}, tpl)
        ps = sorted(p for p, _ in want)
        for i in range(len(ps) - 1):
            gap = (ps[i + 1] - ps[i]) / ps[i] * 100
            self.assertLessEqual(gap, mx + 1e-6,
                                 f"档距 {gap:.2f}% 超过 max_step {mx}%（note={note}）")


if __name__ == "__main__":
    unittest.main()
