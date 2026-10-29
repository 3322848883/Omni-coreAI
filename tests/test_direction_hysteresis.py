# -*- coding: utf-8 -*-
"""方向迟滞带（人格 v5 §2.2）：4H 收盘 vs EMA200 × 0.98 / × 1.005，带内维持原方向。

为什么要有：引擎原来的 `_auto_direction` 是裸判（`close >= ema200` 就翻），
价格贴着均线抖动 0.1% 就每轮翻多翻空 → 同时持多梯 + 空梯、保证金翻倍、
两个方向 SL 互砍。迟滞带把翻向推迟到真突破。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor, _hysteresis_side  # noqa: E402

EMA = 2454.0
LONG_BAND = EMA * 0.98      # 2404.92
SHORT_BAND = EMA * 1.005    # 2466.27


def _db(rows):
    """rows: [(interval, t, close, ema200, boll20, ema20, ema50)]"""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute(
        "CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
        "o REAL, h REAL, l REAL, c REAL, ema200 REAL, ema20 REAL, ema50 REAL, "
        "boll20 REAL)")
    for iv, t, c, e200, boll, e20, e50 in rows:
        con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("ETH_USDT", iv, t, 1, c, c, c, c, e200, e20, e50, boll))
    con.commit()
    con.close()
    return f.name


class C:
    def get_last_price(self, sym):
        return EMA

    def get_contract(self, sym):
        class M:
            quanto_multiplier = 0.01
            size_min = 0.1
            size_step = 0.1
            order_price_round = 0.05
        return M()


class TestHysteresisPure(unittest.TestCase):
    """纯判定：带内维持 / 越带才翻 / 无状态首次。"""

    def test_init_without_prev(self):
        self.assertEqual(_hysteresis_side(2500.0, EMA, None)[0], "long")
        self.assertEqual(_hysteresis_side(2400.0, EMA, None)[0], "short")

    def test_hold_long_inside_band(self):
        # 0.99 × EMA 落在 [0.98, 1.005] 带内 → 维持多
        side, reason = _hysteresis_side(EMA * 0.99, EMA, "long")
        self.assertEqual(side, "long")
        self.assertIn("hold_long", reason)

    def test_flip_short_when_long_band_broken(self):
        side, reason = _hysteresis_side(EMA * 0.979, EMA, "long")
        self.assertEqual(side, "short")
        self.assertIn("flip_short", reason)
        self.assertLess(EMA * 0.979, LONG_BAND)

    def test_hold_short_inside_band(self):
        # 1.004 × EMA 仍在带内（< 1.005）→ 维持空
        side, reason = _hysteresis_side(EMA * 1.004, EMA, "short")
        self.assertEqual(side, "short")
        self.assertIn("hold_short", reason)

    def test_flip_long_when_short_band_broken(self):
        side, reason = _hysteresis_side(EMA * 1.006, EMA, "short")
        self.assertEqual(side, "long")
        self.assertIn("flip_long", reason)
        self.assertGreater(EMA * 1.006, SHORT_BAND)

    def test_hold_long_far_above(self):
        # 远在均线上方：多头当然保持（不是「离开带就翻」）
        self.assertEqual(_hysteresis_side(EMA * 1.05, EMA, "long")[0], "long")

    def test_bad_ema_returns_none(self):
        self.assertEqual(_hysteresis_side(2400.0, 0.0, "long")[0], None)
        self.assertEqual(_hysteresis_side(2400.0, -1.0, "short")[0], None)

    def test_chop_sequence_hysteresis_never_flips(self):
        """核心回归：均线附近 ±0.15% 抖 6 次，迟滞后方向不变。"""
        prices = [2454.5, 2452.0, 2455.0, 2451.0, 2454.8, 2450.5]
        prev, sides = None, []
        for px in prices:
            side, _ = _hysteresis_side(px, EMA, prev)
            sides.append(side)
            prev = side
        self.assertEqual(set(sides), {"long"}, f"带内不得翻向，实际 {sides}")

    def test_chop_sequence_naive_would_flip_repeatedly(self):
        """对照：同一序列裸判会翻 5 次 —— 这就是加迟滞的理由。"""
        prices = [2454.5, 2452.0, 2455.0, 2451.0, 2454.8, 2450.5]
        naive = ["long" if p >= EMA else "short" for p in prices]
        flips = sum(1 for a, b in zip(naive, naive[1:]) if a != b)
        self.assertGreaterEqual(flips, 4, f"裸判应反复翻向，实际 {naive}")


class TestAutoDirectionIntegration(unittest.TestCase):
    """`_auto_direction` 接入迟滞带：状态落盘 / 带内保持 / 回退路径。"""

    def _run(self, rows, dyn_iv="15m", hyst=True, state=None, tpl_extra=None):
        db = _db(rows)
        st = Path(state or (Path(tempfile.mkdtemp()) / "dirstate.json"))
        ex = Executor(C(), symbols_whitelist=["ETH_USDT"])
        tpl = {"direction_mode": "auto",
               "direction_hysteresis": {"enabled": hyst, "interval": "4h",
                                        "long_keep_ratio": 0.98,
                                        "short_keep_ratio": 1.005,
                                        "state_file": str(st)}}
        tpl.update(tpl_extra or {})
        dyn = {"interval": dyn_iv, "db_path": db}
        side, note = ex._auto_direction("ETH_USDT", EMA, dyn, tpl)
        Path(db).unlink(missing_ok=True)
        return side, note, st

    def _rows(self, close, ema200=EMA):
        return [("4h", 1793246400, close, ema200, 2335.0, 2340.0, 2330.0),
                ("15m", 1793272500, close, ema200, 2335.0, 2340.0, 2330.0)]

    def test_first_call_inits_and_writes_state(self):
        side, note, st = self._run(self._rows(2400.0))    # 在 EMA 下方
        self.assertEqual(side, "short")
        self.assertEqual(note["rule"], "ema200_hysteresis")
        self.assertIn("init_short", note["reason"])
        data = json.loads(st.read_text(encoding="utf-8"))
        self.assertEqual(data["ETH_USDT"]["side"], "short")
        self.assertAlmostEqual(data["ETH_USDT"]["ema200"], EMA, places=2)

    def test_keeps_side_inside_band_across_rounds(self):
        st = Path(tempfile.mkdtemp()) / "dirstate.json"
        side1, _, _ = self._run(self._rows(2400.0), state=st)
        # 下一轮价格反弹到 EMA 附近（仍在迟滞带内）→ 不许翻多
        side2, note2, _ = self._run(self._rows(EMA * 1.003), state=st)
        self.assertEqual(side1, "short")
        self.assertEqual(side2, "short", f"带内必须维持原方向，note={note2}")
        self.assertEqual(note2["prev_side"], "short")
        self.assertIn("hold_short", note2["reason"])

    def test_flip_updates_state(self):
        st = Path(tempfile.mkdtemp()) / "dirstate.json"
        self._run(self._rows(2400.0), state=st)                     # init short
        side, note, _ = self._run(self._rows(EMA * 1.01), state=st)  # 涨破 1.005
        self.assertEqual(side, "long")
        self.assertIn("flip_long", note["reason"])
        data = json.loads(st.read_text(encoding="utf-8"))
        self.assertEqual(data["ETH_USDT"]["side"], "long")

    def test_fallback_when_no_4h_row(self):
        rows = [("15m", 1793272500, 2400.0, EMA, 2335.0, 2340.0, 2330.0)]
        side, note, _ = self._run(rows)
        self.assertEqual(note["rule"], "bb_mid+ema", f"应回退裸判，note={note}")
        self.assertIn("hysteresis_fallback", note)

    def test_disabled_uses_bb_rule(self):
        side, note, _ = self._run(self._rows(2400.0), hyst=False)
        self.assertEqual(note["rule"], "bb_mid+ema")
        self.assertNotIn("hysteresis_fallback", note)


if __name__ == "__main__":
    unittest.main()
