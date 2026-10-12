"""_align_size_to_risk 钳制日志回归测试。

P0 观测补丁（martin-grid 6h 体检）：钳制此前只把 note 写进 StepResult.detail，
日志零痕迹 —— 产线 29 张/754U 被静默钳成 11 张/275U。补丁后钳制必须打 WARNING。

同时锁定钳制公式的数值行为（马丁真加码路线依赖这些边界）：
    expected = equity × risk_pct ÷ dist；size_usd > expected×1.3 才钳。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402


class _StubAccount:
    def __init__(self, total):
        self._total = total

    def get_account(self):
        return {"total": self._total}


def _exec(risk_pct=None, equity=72.0, max_notional_usd=None) -> Executor:
    """绕过 __init__：_align_size_to_risk 只依赖这四个属性。"""
    ex = Executor.__new__(Executor)
    ar = {}
    if risk_pct is not None:
        ar["risk_pct"] = risk_pct
    ex.account_risk = ar
    ex.client = _StubAccount(equity)
    ex.max_notional_usd = max_notional_usd
    return ex


def _intent(size_usd, sl, entry, symbol="BTC_USDT"):
    from omnialpha.schema import Intent
    return Intent(
        action="open_long", symbol=symbol, price=entry,
        size_usd=size_usd, sl=sl,
    )


class TestRiskAlignClamp(unittest.TestCase):
    def test_clamp_fires_and_logs_warning(self):
        """超 30% 偏差 → 钳到 expected，且必须打 WARNING（P0 补丁的核心断言）。"""
        ex = _exec(risk_pct=0.01, equity=72.0)
        # entry 2490 / sl 2345 → dist≈5.82% → expected = 72×0.01/0.0582 ≈ 12.4
        # size 149 > 12.4×1.3 → 钳到 12.4
        it = _intent(size_usd=149.0, sl=2345.0, entry=2490.0)
        with self.assertLogs("omnialpha.executor", level="WARNING") as cm:
            out, note = ex._align_size_to_risk(it, 2490.0)
        self.assertAlmostEqual(out, 72.0 * 0.01 / (145 / 2490), places=1)
        self.assertIn("size_clamped", note)
        self.assertTrue(any("RISK_ALIGN" in m and "size_clamped" in m for m in cm.output),
                        cm.output)

    def test_within_tolerance_not_clamped(self):
        """≤ expected×1.3 → 不钳、无日志。"""
        ex = _exec(risk_pct=0.10, equity=72.0)
        # expected = 72×0.10/0.0582 ≈ 123.7；123 < 123.7×1.3 → 放行（马丁真加码边界）
        it = _intent(size_usd=123.0, sl=2345.0, entry=2490.0)
        with self.assertNoLogs("omnialpha.executor", level="WARNING"):
            out, note = ex._align_size_to_risk(it, 2490.0)
        self.assertAlmostEqual(out, 123.0)
        self.assertEqual(note, "")

    def test_rp_012_releases_full_martin_ladder(self):
        """rp=0.12 时三档马丁全部放行 —— 真加码路线的配置依据。

        用 6h 体检的实测计划值：28/160/186/239U（此前被 rp=0.01 全部钳掉）。
        马 1 最贴边：160U @ dist 6.0% 需要 rp ≥ 160×0.06÷(72×1.3) ≈ 0.103，
        0.12 留 20% 余量。
        """
        ex = _exec(risk_pct=0.12, equity=72.0)
        cases = [
            (28.0, 2526.0),   # 网格 L1
            (160.0, 2495.0),  # 马 1（最贴边档）
            (186.0, 2441.0),  # 马 2
            (239.0, 2370.0),  # 马 3
        ]
        for size, entry in cases:
            it = _intent(size_usd=size, sl=2345.0, entry=entry)
            out, note = ex._align_size_to_risk(it, entry)
            self.assertEqual(out, size, f"size={size} entry={entry} 被钳: {note}")

    def test_wide_sl_clamps_gracefully_not_to_one_contract(self):
        """SL 极端拉宽（dist 8%）时马 1 触发钳制，但钳到 ≈108U（≈4 张），不再清零。

        对照 rp=0.01 时代：expected≈12U → sizing 托底抬成 1 张，加码语义归零。
        """
        ex = _exec(risk_pct=0.12, equity=72.0)
        it = _intent(size_usd=149.0, sl=2323.0, entry=2526.0)  # dist = 8%
        out, note = ex._align_size_to_risk(it, 2526.0)
        expected = 72.0 * 0.12 / (203 / 2526)
        self.assertAlmostEqual(out, expected, places=1)
        self.assertGreater(out, 100.0)  # 远大于 1 张（≈25U）
        self.assertIn("size_clamped", note)

    def test_rp_004_still_clamps_martin1(self):
        """rp=0.04 挡不住马 1（149U）—— 证明此前 0.03-0.04 的估计不成立。"""
        ex = _exec(risk_pct=0.04, equity=72.0)
        it = _intent(size_usd=149.0, sl=2345.0, entry=2490.0)
        out, note = ex._align_size_to_risk(it, 2490.0)
        self.assertIn("size_clamped", note)
        self.assertLess(out, 60.0)

    def test_no_sl_passthrough(self):
        """无 SL → 原样放行（风险公式无从计算）。"""
        from types import SimpleNamespace
        ex = _exec(risk_pct=0.01)
        it = SimpleNamespace(action="open_long", symbol="BTC_USDT",
                             size_usd=500.0, sl=None)
        out, note = ex._align_size_to_risk(it, 2490.0)
        self.assertEqual(out, 500.0)
        self.assertEqual(note, "")


if __name__ == "__main__":
    unittest.main()
