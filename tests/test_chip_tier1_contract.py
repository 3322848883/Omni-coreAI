"""Chip 契约 Tier 1：7 个 optional 字段 + 区间校验（S2）。

设计要点（见 docs/compose/spec/pa-skills-upgrade.md [S2]）：
- 7 个字段全部 optional，缺省不改变任何现有行为（对正在跑的 bot 零影响）；
- `region == "range"` 时不得给 `tp2` —— 把提示词「区域=区间 → 禁止持有 2R 目标」
  从一句话变成程序约束。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.schema import Chip, PlanError, parse_plan  # noqa: E402


def _chip(**over) -> dict:
    base = {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.7, "size_usd": 100}
    base.update(over)
    return base


class TestTier1Fields(unittest.TestCase):
    def test_new_fields_parsed(self):
        plan = parse_plan({"chips": [_chip(
            region="trend",
            invalidation=85560.0,
            time_stop_bars=8,
            give_back_pct=40.0,
            risk_pct=1.4,
            rule_ids=["SB-06", "SA-05"],
            scenarios={"entry_pending": "hold", "position_open": "trail"},
        )]})
        c = plan.chips[0]
        self.assertEqual(c.region, "trend")
        self.assertEqual(c.invalidation, 85560.0)
        self.assertEqual(c.time_stop_bars, 8)
        self.assertEqual(c.give_back_pct, 40.0)
        self.assertEqual(c.risk_pct, 1.4)
        self.assertEqual(c.rule_ids, ["SB-06", "SA-05"])
        self.assertEqual(c.scenarios["entry_pending"], "hold")

    def test_new_fields_passed_through_to_signal(self):
        plan = parse_plan({"chips": [_chip(
            region="trend", invalidation=85560.0, rule_ids=["SB-06"], risk_pct=1.4,
        )]})
        d = plan.chips[0].to_signal_dict()
        self.assertEqual(d["region"], "trend")
        self.assertEqual(d["invalidation"], 85560.0)
        self.assertEqual(d["rule_ids"], ["SB-06"])
        self.assertEqual(d["risk_pct"], 1.4)

    def test_absent_fields_keep_old_behavior(self):
        """旧 Plan JSON（无新字段）解析与透传都不变。"""
        plan = parse_plan({"chips": [_chip(tp=86000.0, sl=85500.0)]})
        c = plan.chips[0]
        self.assertEqual(c.region, "")
        self.assertIsNone(c.invalidation)
        self.assertIsNone(c.time_stop_bars)
        self.assertIsNone(c.give_back_pct)
        self.assertIsNone(c.risk_pct)
        self.assertEqual(c.rule_ids, [])
        self.assertEqual(c.scenarios, {})
        d = c.to_signal_dict()
        for k in ("region", "invalidation", "time_stop_bars", "give_back_pct",
                  "risk_pct", "rule_ids", "scenarios"):
            self.assertNotIn(k, d, f"未设置时不应透传 {k}")

    def test_invalid_region_rejected(self):
        with self.assertRaises(PlanError) as cm:
            parse_plan({"chips": [_chip(region="up")]})
        self.assertIn("region", str(cm.exception))


class TestRangeForbidsTp2(unittest.TestCase):
    def test_range_with_tp2_rejected(self):
        with self.assertRaises(PlanError) as cm:
            parse_plan({"chips": [_chip(region="range", tp=86150.0, tp2=86600.0)]})
        self.assertIn("tp2", str(cm.exception))

    def test_range_without_tp2_ok(self):
        plan = parse_plan({"chips": [_chip(region="range", tp=86150.0, sl=85560.0)]})
        self.assertEqual(plan.chips[0].region, "range")
        self.assertIsNone(plan.chips[0].tp2)

    def test_trend_with_tp2_ok(self):
        plan = parse_plan({"chips": [_chip(region="trend", tp=86150.0, tp2=86600.0)]})
        self.assertEqual(plan.chips[0].tp2, 86600.0)

    def test_absent_region_with_tp2_ok(self):
        """未声明区域时不做校验 —— 保持向后兼容。"""
        plan = parse_plan({"chips": [_chip(tp=86150.0, tp2=86600.0)]})
        self.assertEqual(plan.chips[0].tp2, 86600.0)

    def test_reversal_with_tp2_ok(self):
        plan = parse_plan({"chips": [_chip(region="reversal", tp=86150.0, tp2=86600.0)]})
        self.assertEqual(plan.chips[0].region, "reversal")


class TestChipDefaults(unittest.TestCase):
    def test_dataclass_defaults(self):
        c = Chip(symbol="BTC_USDT", action="hold")
        self.assertEqual(c.region, "")
        self.assertIsNone(c.invalidation)
        self.assertIsNone(c.time_stop_bars)
        self.assertIsNone(c.give_back_pct)
        self.assertIsNone(c.risk_pct)
        self.assertEqual(c.rule_ids, [])
        self.assertEqual(c.scenarios, {})


class TestBadInputRaisesPlanError(unittest.TestCase):
    """坏输入必须抛 `PlanError`，不能漏出 `ValueError`/`TypeError`。

    实测动因（独立评审 Critical 2）：调用方**只捕 `PlanError`**（`loop.py` 的
    `except PlanError`、`__main__.cmd_plan` 连 try 都没有）。直转 `int()`/`float()`
    会把 `ValueError` 漏出去 → `plan` 命令 traceback；`plan-loop` 虽被外层兜住，
    但会**绕过 `degraded` 与 `_record_cycle_failure`** → `plan_fail` 告警静默不计数。

    暴露面是**新增**的：改动前 `time_stop_bars` 这类字段根本不被解析（写什么都忽略），
    而规则 18 现在要求模型填 `give_back_pct`/`risk_pct`（语义带 `(%)`，写 `"40%"` 即命中）。
    """

    def test_int_field_with_suffix(self):
        with self.assertRaises(PlanError):
            parse_plan({"chips": [_chip(time_stop_bars="8bars")]})

    def test_int_field_with_list(self):
        with self.assertRaises(PlanError):
            parse_plan({"chips": [_chip(time_stop_bars=[])]})

    def test_float_field_with_text(self):
        with self.assertRaises(PlanError):
            parse_plan({"chips": [_chip(risk_pct="abc")]})

    def test_float_field_with_percent_sign(self):
        """规则 18 把 `give_back_pct` 描述成「浮盈回撤阈值(%)」—— 模型可能写 `40%`。"""
        with self.assertRaises(PlanError):
            parse_plan({"chips": [_chip(give_back_pct="40%")]})

    def test_existing_float_fields_also_guarded(self):
        """老字段（tp/sl/size_usd…）此前也会漏 ValueError，一并收口。"""
        for field in ("tp", "sl", "tp2", "price", "size_usd"):
            with self.subTest(field=field):
                with self.assertRaises(PlanError):
                    parse_plan({"chips": [_chip(**{field: "not-a-number"})]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
