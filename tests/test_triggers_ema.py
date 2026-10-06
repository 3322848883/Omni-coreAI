"""EMA 家族触发器：`price_cross_ema` / `ema_stack` / `price_ema_dist` / `ema_slope`。

补齐的原因（2026-10-06 线上实测）：原先 9 个类型里**没有任何一个表达「价格穿越 EMA」** ——
`price_vs_ema` 是**状态**（价格在 EMA 上/下，只看当前、不比较上一根），`ema_cross` 是
**快 EMA 穿慢 EMA**（跟价格无关）。模型想表达「价格穿 EMA20 时叫醒我」时只能退而选
`price_vs_ema`，而状态型配冷却在数学上等于一个定时器 —— 实测一个 bot 因此每 5 分钟
被唤醒一次，占掉 43% 的轮次（35/82）。

这里同时钉住「状态型 vs 事件型」的差别：同一个上升序列，`price_vs_ema` 为真，
而 `price_cross_ema` 为假（因为上一根也在上方，没有发生穿越）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.trigger_store import (  # noqa: E402
    AITriggerPolicy,
    TriggerPolicyError,
    validate_trigger_payload,
)
from omnialpha.strategist.triggers import evaluate_condition  # noqa: E402


class FakeMD:
    """与 tests/test_triggers.py 的桩件同形。"""

    def __init__(self, closes=None):
        self.closes = closes or [float(i) for i in range(1, 40)]

    def public_get(self, path, qs=""):
        rows = []
        for i, c in enumerate(self.closes):
            t = 1000 + i * 60
            rows.append([t, "1", str(c), str(c + 1), str(c - 1), str(c - 0.5), "0"])
        return rows


ASC = [float(i) for i in range(1, 40)]                 # 单调上升
DESC = [float(i) for i in range(39, 0, -1)]            # 单调下降
UP_CROSS = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 20]         # 末根上穿
DOWN_CROSS = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 1]   # 末根下穿


def ev(closes, **cond):
    cond.setdefault("symbol", "BTC_USDT")
    return evaluate_condition(FakeMD(closes), cond, "1m")


class TestPriceCrossEma(unittest.TestCase):
    """事件型：只在**穿越那一刻**为真。"""

    def test_up_cross_fires(self):
        ok, reason = ev(UP_CROSS, type="price_cross_ema", period=2, dir="up")
        self.assertTrue(ok, reason)
        self.assertIn("cross up", reason)

    def test_down_cross_fires(self):
        ok, reason = ev(DOWN_CROSS, type="price_cross_ema", period=2, dir="down")
        self.assertTrue(ok, reason)
        self.assertIn("cross down", reason)

    def test_uptrend_does_not_fire(self):
        """**关键对照**：单调上升时价格一直在 EMA 上方，但并没有「穿越」→ 不该触发。"""
        ok, _ = ev(ASC, type="price_cross_ema", period=5, dir="up")
        self.assertFalse(ok)

    def test_same_series_price_vs_ema_is_true(self):
        """同一序列 `price_vs_ema` 为真 —— 这正是两者的区别（状态 vs 事件）。"""
        ok, _ = ev(ASC, type="price_vs_ema", period=5, side="above")
        self.assertTrue(ok)

    def test_wrong_direction_does_not_fire(self):
        ok, _ = ev(UP_CROSS, type="price_cross_ema", period=2, dir="down")
        self.assertFalse(ok)

    def test_any_fires_on_either(self):
        ok_up, _ = ev(UP_CROSS, type="price_cross_ema", period=2, dir="any")
        ok_dn, _ = ev(DOWN_CROSS, type="price_cross_ema", period=2, dir="any")
        self.assertTrue(ok_up and ok_dn)

    def test_default_dir_is_any(self):
        ok, _ = ev(UP_CROSS, type="price_cross_ema", period=2)
        self.assertTrue(ok)

    def test_too_few_bars(self):
        ok, reason = ev([10.0], type="price_cross_ema", period=2)
        self.assertFalse(ok)
        # 单根 K 线会先被更早的「not enough candles」守卫拦下，到不了 ema not ready
        self.assertIn("not enough", reason)


class TestEmaStack(unittest.TestCase):
    """状态型：价格 + 双 EMA 的排列。"""

    def test_bull_stack(self):
        ok, reason = ev(ASC, type="ema_stack", fast=5, slow=10, dir="bull")
        self.assertTrue(ok, reason)

    def test_bear_stack(self):
        ok, reason = ev(DESC, type="ema_stack", fast=5, slow=10, dir="bear")
        self.assertTrue(ok, reason)

    def test_bull_not_true_on_downtrend(self):
        ok, _ = ev(DESC, type="ema_stack", fast=5, slow=10, dir="bull")
        self.assertFalse(ok)

    def test_any_direction(self):
        ok_up, _ = ev(ASC, type="ema_stack", fast=5, slow=10, dir="any")
        ok_dn, _ = ev(DESC, type="ema_stack", fast=5, slow=10, dir="any")
        self.assertTrue(ok_up and ok_dn)


class TestPriceEmaDist(unittest.TestCase):
    """状态型：价格偏离 EMA 超过 pct%。"""

    def test_above_threshold(self):
        closes = [10.0] * 20 + [20.0]     # 末根远离 EMA
        ok, reason = ev(closes, type="price_ema_dist", period=5, pct=5.0, side="above")
        self.assertTrue(ok, reason)
        self.assertIn("dist=", reason)

    def test_below_threshold_not_fired(self):
        closes = [10.0] * 20 + [20.0]
        ok, _ = ev(closes, type="price_ema_dist", period=5, pct=200.0, side="above")
        self.assertFalse(ok)

    def test_below_side(self):
        closes = [20.0] * 20 + [5.0]
        ok, reason = ev(closes, type="price_ema_dist", period=5, pct=5.0, side="below")
        self.assertTrue(ok, reason)

    def test_flat_series_no_fire(self):
        ok, _ = ev([10.0] * 30, type="price_ema_dist", period=5, pct=1.0, side="above")
        self.assertFalse(ok)

    def test_pct_zero_rejected_at_eval(self):
        ok, reason = ev(ASC, type="price_ema_dist", period=5, pct=0, side="above")
        self.assertFalse(ok)
        self.assertIn("pct", reason)


class TestEmaSlope(unittest.TestCase):
    """状态型：EMA 在最近 bars 根内上行/下行。"""

    def test_up_slope(self):
        ok, reason = ev(ASC, type="ema_slope", period=5, bars=3, dir="up")
        self.assertTrue(ok, reason)

    def test_down_slope(self):
        ok, reason = ev(DESC, type="ema_slope", period=5, bars=3, dir="down")
        self.assertTrue(ok, reason)

    def test_up_not_true_on_downtrend(self):
        ok, _ = ev(DESC, type="ema_slope", period=5, bars=3, dir="up")
        self.assertFalse(ok)

    def test_flat_series_neither(self):
        ok_up, _ = ev([10.0] * 30, type="ema_slope", period=5, bars=3, dir="up")
        ok_dn, _ = ev([10.0] * 30, type="ema_slope", period=5, bars=3, dir="down")
        self.assertFalse(ok_up or ok_dn)


class TestPolicyRegistration(unittest.TestCase):
    """新类型必须注册进 allow，否则模型写了会被整条拒掉。"""

    def _policy(self):
        return AITriggerPolicy(enabled=True, default_cooldown_sec=300.0)

    def test_all_four_types_allowed(self):
        pol = self._policy()
        cases = (
            ("price_cross_ema", {"period": 20, "dir": "up"}, "period", 20),
            ("ema_stack", {"fast": 20, "slow": 50, "dir": "bull"}, "fast", 20),
            ("price_ema_dist", {"period": 20, "pct": 1.5, "side": "above"}, "pct", 1.5),
            ("ema_slope", {"period": 20, "bars": 3, "dir": "up"}, "bars", 3),
        )
        for t, params, key, want in cases:
            out = validate_trigger_payload(
                {"type": t, "symbol": "BTC_USDT", **params},
                pol, bot_symbols=["BTC_USDT"])
            self.assertEqual(out["type"], t)
            self.assertEqual(out["params"][key], want,
                             f"{t} 的 {key} 被参数白名单丢掉了")

    def test_pct_and_bars_survive_whitelist(self):
        """`pct`/`bars` 必须在参数白名单里，否则会被静默丢弃（等于没设）。"""
        out = validate_trigger_payload(
            {"type": "price_ema_dist", "symbol": "BTC_USDT", "period": 20,
             "pct": 1.5, "side": "above"},
            self._policy(), bot_symbols=["BTC_USDT"])
        self.assertEqual(out["params"]["pct"], 1.5)
        out2 = validate_trigger_payload(
            {"type": "ema_slope", "symbol": "BTC_USDT", "period": 20,
             "bars": 3, "dir": "up"},
            self._policy(), bot_symbols=["BTC_USDT"])
        self.assertEqual(out2["params"]["bars"], 3)

    def test_pct_out_of_range_rejected(self):
        with self.assertRaises(TriggerPolicyError):
            validate_trigger_payload(
                {"type": "price_ema_dist", "symbol": "BTC_USDT", "period": 20, "pct": 999},
                self._policy(), bot_symbols=["BTC_USDT"])

    def test_bars_out_of_range_rejected(self):
        with self.assertRaises(TriggerPolicyError):
            validate_trigger_payload(
                {"type": "ema_slope", "symbol": "BTC_USDT", "period": 20, "bars": 9999},
                self._policy(), bot_symbols=["BTC_USDT"])


if __name__ == "__main__":
    unittest.main()
