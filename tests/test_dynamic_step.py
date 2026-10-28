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


if __name__ == "__main__":
    unittest.main()
