# -*- coding: utf-8 -*-
"""动态档距（A+C）：ATR% 与 BB 带宽/6 取较大者，clamp + 总跨度封顶。

为什么：固定 0.5% 档距在 ATR 40 时会被一根 K 线穿透 5~7 层（马丁瞬间满仓），
ATR 6 时又太稀不成交。数据源是 v2 库的 atr14 / boll 三件套。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARK0 = 2335.0
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402


class C:
    """最小 client：动态档距只用到 last_price / contract。"""

    def get_last_price(self, sym):
        return 2360.0

    def get_contract(self, sym):
        class M:
            quanto_multiplier = 0.01
            size_min = 0.1
            size_step = 0.1
            order_price_round = 0.05
        return M()


def _db(atr, mid, up, lo, closed=1):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, closed, 2360, 2361, 2359, 2360,
                 atr, mid, up, lo))
    con.commit()
    con.close()
    return f.name


class TestDynamicStep(unittest.TestCase):
    def _ex(self):
        return Executor(C(), symbols_whitelist=["ETH_USDT"])

    def test_uses_max_of_atr_and_bandwidth(self):
        """ATR% 与带宽/6 取大者：ATR 大时按 ATR，带宽大时按带宽。"""
        # ATR% = 23.6/2360 = 1.0% → 1.0；带宽/6 = (2450-2270)/2360/6 = 1.27% → 取 1.27
        db = _db(atr=23.6, mid=2360, up=2450, lo=2270)
        ex = self._ex()
        step, note = ex._dynamic_step("ETH_USDT", 2360.0, 7, 0.5,
                                      {"db_path": db, "max_step_pct": 2.0})
        self.assertAlmostEqual(step, 1.27, places=2)
        self.assertIn("bw_pct", note)

    def test_clamped_by_min_max(self):
        db = _db(atr=1.0, mid=2360, up=2365, lo=2355)   # 很低波动
        ex = self._ex()
        step, _ = ex._dynamic_step("ETH_USDT", 2360.0, 7, 0.5,
                                   {"db_path": db, "min_step_pct": 0.4})
        self.assertAlmostEqual(step, 0.4, places=4, msg="不得低于 min_step_pct")

    def test_span_cap(self):
        db = _db(atr=60.0, mid=2360, up=2500, lo=2200)  # 很高波动
        ex = self._ex()
        step, note = ex._dynamic_step("ETH_USDT", 2360.0, 7, 0.5,
                                      {"db_path": db, "max_step_pct": 3.0,
                                       "max_span_pct": 6.0})
        self.assertLessEqual(step * 7, 6.0 + 1e-9, "总跨度不得超过 6%")
        self.assertAlmostEqual(step, 6.0 / 7, places=4)

    def test_fallback_when_db_missing(self):
        ex = self._ex()
        step, note = ex._dynamic_step("ETH_USDT", 2360.0, 7, 0.5,
                                      {"db_path": "/tmp/does_not_exist.db"})
        self.assertEqual(step, 0.5, "读不到指标 → 回退固定档距")
        self.assertIn("error", note)

    def test_fallback_when_no_indicator_row(self):
        db = _db(atr=None, mid=None, up=None, lo=None)
        ex = self._ex()
        step, note = ex._dynamic_step("ETH_USDT", 2360.0, 7, 0.5, {"db_path": db})
        self.assertEqual(step, 0.5)
        self.assertEqual(note.get("error"), "no_indicator_row")


class TestMartinScaling(unittest.TestCase):
    """马丁同比缩放 + 上限压缩（本地模拟高波动暴露的两个坑）。"""

    def _ex(self):
        return Executor(C(), symbols_whitelist=["ETH_USDT"])

    def _run(self, atr, mid, up, lo, max_martin=9.0):
        db = _db(atr=atr, mid=mid, up=up, lo=lo)
        ex = self._ex()
        tpl = {
            "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5,
                        "bb_div": 6.0, "min_step_pct": 0.45, "max_step_pct": 1.2,
                        "max_span_pct": 6.0, "scale_martin": True,
                        "max_martin_pct": max_martin, "db_path": db},
            "martin": [{"pct": 4.2, "lots": 6}, {"pct": 5.4, "lots": 7},
                       {"pct": 6.6, "lots": 9}],
        }
        ex.ladder_template = tpl

        class Rep:
            results = []

        import omnialpha.executor as E
        leg = E.Intent(action="open_long", symbol="ETH_USDT", price=mid * 0.995,
                       size_usd=mid * 0.01, sl=mid * 0.9)
        c = _C(mid)
        ex.client = c
        rep = Rep()
        ex._complete_ladder([leg], rep)
        return [float(o["price"]) for o in c.placed], rep

    def test_high_vol_martin_compressed_not_clamped(self):
        """高波动：马丁必须压到 max_martin 且三档价格互不相同（10 层全铺）。"""
        prices, rep = self._run(atr=18.2, mid=2417, up=2500, lo=2380, max_martin=9.0)
        self.assertEqual(len(prices), 10, "压缩后仍应铺满 10 层（不能撞价被吞）")
        deepest = (2417 - min(prices)) / 2417 * 100
        self.assertLessEqual(deepest, 9.0 + 0.05, f"最深档 {deepest:.2f}% 超上限")

    def test_scaled_martin_keeps_order(self):
        prices, _ = self._run(atr=18.2, mid=2417, up=2500, lo=2380)
        martin = sorted(prices)[:3]
        self.assertEqual(len(set(round(p, 2) for p in martin)), 3, "三档不得重复")


class _C:
    def __init__(self, mark):
        self.mark = mark
        self.placed = []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        class M:
            quanto_multiplier = 0.01
            size_min = 0.1
            size_step = 0.1
            order_price_round = 0.05
        return M()

    def list_orders(self, sym):
        return []

    def list_price_orders(self, sym):
        return []

    def get_positions(self):
        return []

    def place_order(self, body):
        self.placed.append(body)
        return {"id": f"o{len(self.placed)}"}

    def place_price_order(self, body):
        return {"id": "p1"}

    def cancel_order(self, oid):
        return {}


class TestAiStepHint(unittest.TestCase):
    """AI 档距建议通道：模型可给 ladder_step_pct，引擎 clamp 兜底。"""

    def _run(self, hint_meta=None, hint_intent=None, max_step="auto"):
        db = _db(atr=7.8, mid=MARK0, up=MARK0 * 1.013, lo=MARK0 * 0.987)
        c = _C(MARK0)
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        ex.ladder_template = {
            "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5,
                        "bb_div": 6.0, "min_step_pct": 0.45, "max_step_pct": max_step,
                        "max_span_pct": 6.0, "scale_martin": True,
                        "max_martin_pct": 9.0, "db_path": db},
            "martin": [{"pct": 4.2, "lots": 6}],
        }
        ex._round_meta = dict(hint_meta or {})
        import omnialpha.executor as E
        leg = E.Intent(action="open_long", symbol="ETH_USDT", price=MARK0 * 0.995,
                       size_usd=MARK0 * 0.01, sl=MARK0 * 0.92,
                       meta=dict(hint_intent or {}))
        class Rep:
            results = []
        rep = Rep()
        ex._complete_ladder([leg], rep)
        det = next((x.detail for x in rep.results
                    if getattr(x, "action", "") == "ladder_template" and x.detail), {})
        import pathlib as _pl
        _pl.Path(db).unlink(missing_ok=True)
        return det.get("step_pct"), (det.get("dynamic") or {})

    def test_ai_hint_used_when_in_range(self):
        step, dyn = self._run(hint_meta={"ladder_step_pct": 0.7})
        self.assertAlmostEqual(step, 0.7, places=4)
        self.assertFalse(dyn.get("ai_step_clamped"))

    def test_ai_hint_clamped_by_span(self):
        step, dyn = self._run(hint_meta={"ladder_step_pct": 2.0})
        self.assertAlmostEqual(step, 6.0 / 7, places=3, msg="越界必须被跨度封顶")
        self.assertTrue(dyn.get("ai_step_clamped"))

    def test_ai_hint_clamped_by_min(self):
        step, dyn = self._run(hint_meta={"ladder_step_pct": 0.1})
        self.assertAlmostEqual(step, 0.45, places=4)
        self.assertTrue(dyn.get("ai_step_clamped"))

    def test_ai_hint_from_intent_meta(self):
        step, _ = self._run(hint_intent={"ladder_step_pct": 0.8})
        self.assertAlmostEqual(step, 0.8, places=4)

    def test_no_hint_falls_back_to_dynamic(self):
        step, dyn = self._run()
        self.assertIsNone(dyn.get("ai_step_pct"))
        self.assertAlmostEqual(step, 0.5011, places=3)


if __name__ == "__main__":
    unittest.main()
