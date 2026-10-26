"""paper 下单校验的张数步长与 live 同源（修法 A，2026-10-27）。

背景（ladder-v5-paper 产线实测）：
- `paper/validate.py` 原来用 `order_size_round` 当 lot —— 交易所根本不提供该字段
  （Gate 1061 个合约 0 命中），恒为假值 1 → paper 永远只能整数张。
- 后果 1：live 下的 0.7 张 SL 在 paper 被四舍五入成 1 张（名义/风险全走样）。
- 后果 2：`_cap_combined_risk` 缩放出的 0.43 张被 `round(0.43, 1) = 0` 拒单 →
  整轮失败 → 回滚 → 旧单每轮泄漏堆积（实测 20 条入场 + 11 条 SL）。

修复：lot 优先取 `size_step`（与 `sizing.safe_step` 同源），取整改 **floor**
（对齐交易所对非步长倍数静默 floor 的语义，也与 live `quantize_size` 同口径）；
没有 `size_step` 的旧测试替身退回 `order_size_round`（既有测试行为不变）。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import ContractMeta  # noqa: E402
from omnialpha.paper.validate import PaperReject, validate_and_round_order  # noqa: E402


def _eth_meta():
    """真实口径的 ETH 合约元数据：步长 0.1（enable_decimal），乘数 0.01。"""
    return ContractMeta(
        name="ETH_USDT", quanto_multiplier=0.01,
        order_size_round=1, order_price_round=0.1,
        leverage_max=50, size_min=0.1,
    )


def _body(size, price=2500.0):
    return {"size": size, "price": str(price), "tif": "GTC"}


class TestPaperLotUsesSizeStep(unittest.TestCase):
    def test_fractional_survives(self):
        """★核心回归★ 0.7 张不再被取整成 1 张 —— live 与 paper 同为小数张。"""
        out = validate_and_round_order(_body(-0.7), _eth_meta(), 2500.0)
        self.assertAlmostEqual(out["size"], -0.7)

    def test_floor_not_round_up(self):
        """0.43 张 floor 到 0.4（对齐交易所静默 floor），
        不再出现 `round(0.43, 1) = 0` 式的拒单链。"""
        out = validate_and_round_order(_body(0.43), _eth_meta(), 2500.0)
        self.assertAlmostEqual(out["size"], 0.4)

    def test_floor_negative_side(self):
        out = validate_and_round_order(_body(-0.43), _eth_meta(), 2500.0)
        self.assertAlmostEqual(out["size"], -0.4)

    def test_below_step_rejected(self):
        """不足一个步长（0.07 < 0.1）→ 拒单，静默放大/归零都不行。"""
        with self.assertRaises(PaperReject):
            validate_and_round_order(_body(0.07), _eth_meta(), 2500.0)

    def test_integer_sizes_unchanged(self):
        """整数张（步长的整数倍）原样通过。"""
        out = validate_and_round_order(_body(10), _eth_meta(), 2500.0)
        self.assertAlmostEqual(out["size"], 10.0)


class TestLegacyMetaFallback(unittest.TestCase):
    """旧测试替身没有 `size_step` → 退回 `order_size_round`，行为不变。"""

    def test_legacy_round_1_keeps_old_behavior(self):
        meta = SimpleNamespace(
            quanto_multiplier=1.0, order_size_round=1.0,
            order_price_round=0.1, leverage_max=100,
        )
        out = validate_and_round_order(_body(0.7, price=100.0), meta, 100.0)
        self.assertAlmostEqual(out["size"], 1.0)

    def test_no_lot_info_passes_through(self):
        meta = SimpleNamespace(
            quanto_multiplier=1.0, order_size_round=0,
            order_price_round=0.1, leverage_max=100,
        )
        out = validate_and_round_order(_body(0.7, price=100.0), meta, 100.0)
        self.assertAlmostEqual(out["size"], 0.7)


if __name__ == "__main__":
    unittest.main()
