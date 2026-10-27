# -*- coding: utf-8 -*-
"""组合风险缩放的最小单位保护（2026-10-28 实测事故回归）。

事故链：`_cap_combined_risk` 等比缩放 → 网格 1 张变 0.92 张 → floor 0.9 张 →
Gate `orders` 端点对 ETH 按整数张 floor → **0 张挂单**（19:53 轮 7 条 size=0）。

规则：折算张数 ≤1 的腿跳过缩放（1 张已是最小可交易单位）；缩到 <1 张的回退 1 张。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1


class C:
    def get_account(self):
        return {"total": "100"}          # 权益 100U

    def get_contract(self, symbol):
        return Meta()


class TestCapScaleMinUnit(unittest.TestCase):
    def _ex(self, total_risk_pct):
        return Executor(C(), symbols_whitelist=["ETH_USDT"],
                        account_risk={"total_risk_pct": total_risk_pct})

    def test_grid_one_lot_not_scaled(self):
        """网格 1 张（size_usd = price×0.01）不能被缩放 —— 否则变 0 张。"""
        ex = self._ex(0.05)   # 5% × 100U = 5U 风险上限，迫使缩
        # 网格 1 张 + 马丁 9 张，止损距离同为 8%（2165 vs 2332）
        grid = Intent(action="open_long", symbol="ETH_USDT", price=2332.0,
                      size_usd=23.32, sl=2165.0)
        martin = Intent(action="open_long", symbol="ETH_USDT", price=2206.5,
                        size_usd=198.585, sl=2165.0)
        out, note = ex._cap_combined_risk([grid, martin])
        self.assertEqual(out[0].size_usd, 23.32, "网格 1 张腿必须原样保留")
        self.assertLess(out[1].size_usd, 198.585, "马丁腿应被缩")
        # 马丁缩后仍 ≥1 张
        c = out[1].size_usd / (2206.5 * 0.01)
        self.assertGreaterEqual(c, 1.0)
        self.assertIn("≤1 张腿未缩", note)

    def test_all_small_legs_skip_scaling(self):
        ex = self._ex(0.05)
        legs = [Intent(action="open_long", symbol="ETH_USDT", price=2332.0 - i * 10,
                       size_usd=(2332.0 - i * 10) * 0.01, sl=2165.0) for i in range(7)]
        out, note = ex._cap_combined_risk(legs)
        self.assertEqual([i.size_usd for i in out], [i.size_usd for i in legs],
                         "全是 1 张腿时一律不缩")
        self.assertIn("跳过缩放", note)

    def test_scale_never_below_one_lot(self):
        ex = self._ex(0.005)   # 更强缩放
        m = Intent(action="open_long", symbol="ETH_USDT", price=2206.5,
                   size_usd=198.585, sl=2165.0)      # 9 张，距离 1.88% → 风险 3.7U
        out, _ = ex._cap_combined_risk([m])
        c = out[0].size_usd / (2206.5 * 0.01)
        self.assertGreaterEqual(c, 0.999, "缩到 <1 张应回退为 1 张")


if __name__ == "__main__":
    unittest.main()
