# -*- coding: utf-8 -*-
"""给 AI 的预算必须与执行器的权益闸门一致。

执行器硬闸门：单笔名义 ≤ 权益 × max_notional_pct（executor._check_notional）。
若只把静态配置值交给 AI，权益一跌闸门收紧，AI 仍按旧预算出价 → 整笔被拒、白烧一轮。
"""
from __future__ import annotations

import unittest

from omnialpha.strategist.loop import PlanRunner, StrategistConfig
from omnialpha.strategist.risk import RiskConfig


def _runner(max_notional=None, account_risk=None) -> PlanRunner:
    r = PlanRunner.__new__(PlanRunner)
    r.cfg = StrategistConfig(
        symbols=["BTC_USDT"],
        risk=RiskConfig(max_notional_usd=max_notional),
        account_risk=dict(account_risk or {}),
    )
    return r


class TestPromptBudget(unittest.TestCase):
    def test_gate_wins_when_smaller(self):
        """实盘现状：配置 10000，权益 90 → 闸门 450，应给 450。"""
        r = _runner(max_notional=10000, account_risk={"max_notional_pct": 5.0})
        out = r._prompt_risk({"account": {"total": 90.0}})
        self.assertEqual(out["max_notional_usd"], 450.0)

    def test_config_wins_when_smaller(self):
        """配置比闸门小 → 用配置（闸门不该放宽预算）。"""
        r = _runner(max_notional=100, account_risk={"max_notional_pct": 5.0})
        out = r._prompt_risk({"account": {"total": 1000.0}})
        self.assertEqual(out["max_notional_usd"], 100.0)

    def test_default_pct_is_five(self):
        """未配 max_notional_pct → 默认 5.0（与 executor 一致）。"""
        r = _runner(max_notional=100000, account_risk={})
        out = r._prompt_risk({"account": {"total": 10000.0}})
        self.assertEqual(out["max_notional_usd"], 50000.0)

    def test_none_config_falls_back_to_gate(self):
        """配置未设 → 直接用闸门值。"""
        r = _runner(max_notional=None, account_risk={"max_notional_pct": 5.0})
        out = r._prompt_risk({"account": {"total": 200.0}})
        self.assertEqual(out["max_notional_usd"], 1000.0)

    def test_equity_missing_keeps_config(self):
        """取不到权益 → 保持配置值，不要算成 0 把预算掐死。"""
        r = _runner(max_notional=400, account_risk={"max_notional_pct": 5.0})
        for snap in ({}, {"account": {}}, {"account": {"error": "down"}},
                     {"account": {"total": None}}, {"account": {"total": "abc"}}):
            with self.subTest(snap=snap):
                self.assertEqual(r._prompt_risk(snap)["max_notional_usd"], 400)

    def test_shrinks_with_equity_drawdown(self):
        """权益下跌 → 预算同步收缩（这正是配置对齐没覆盖的情形）。"""
        r = _runner(max_notional=50000, account_risk={"max_notional_pct": 5.0})
        self.assertEqual(r._prompt_risk({"account": {"total": 10000.0}})["max_notional_usd"], 50000.0)
        self.assertEqual(r._prompt_risk({"account": {"total": 7780.0}})["max_notional_usd"], 38900.0)

    def test_zero_or_bad_pct_ignored(self):
        """pct 为 0/负数/非法 → 视为不设比例闸门，保持配置值。"""
        for bad in (0, -1, "x"):
            with self.subTest(pct=bad):
                r = _runner(max_notional=400, account_risk={"max_notional_pct": bad})
                self.assertEqual(r._prompt_risk({"account": {"total": 90.0}})["max_notional_usd"], 400)

    def test_other_risk_fields_preserved(self):
        r = _runner(max_notional=400, account_risk={"max_notional_pct": 5.0})
        r.cfg.risk.min_confidence = 0.62
        r.cfg.risk.max_chips = 2
        out = r._prompt_risk({"account": {"total": 90.0}})
        self.assertEqual(out["min_confidence"], 0.62)
        self.assertEqual(out["max_chips"], 2)


class TestRunOnceUsesEffectiveBudget(unittest.TestCase):
    """确认 run_once 真的把有效预算交给了 prompt，而不是仍用配置值。"""

    def test_run_once_passes_effective_budget(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from omnialpha.strategist import loop as loopmod

        with tempfile.TemporaryDirectory() as td:
            r = PlanRunner(
                client=object(),
                cfg=StrategistConfig(
                    symbols=["BTC_USDT"], vision=False, prompt_file="",
                    bot_root=Path(td), bot_id="bot-a", env="paper",
                    risk=RiskConfig(max_notional_usd=10000),
                    account_risk={"max_notional_pct": 5.0},
                ),
                inbox=Path(td) / "inbox", history_dir=Path(td) / "state", llm=object(),
            )
            r._chat_with_tools = lambda *a, **kw: (
                '{"cycle_id":"c1","chips":[{"symbol":"BTC_USDT","action":"hold","confidence":0.0}]}'
            )
            seen = {}

            def fake_prompt(snapshot, risk, symbols):
                seen.update(risk)
                return "usr"

            with mock.patch.object(loopmod, "collect_snapshot",
                                   return_value={"account": {"total": 90.0}}), \
                 mock.patch.object(loopmod, "build_system_prompt", return_value="sys"), \
                 mock.patch.object(loopmod, "build_user_prompt", side_effect=fake_prompt):
                r.run_once(trigger="test")

            self.assertEqual(seen["max_notional_usd"], 450.0,
                             "run_once 必须把「权益×比例」算进去再交给 AI")


if __name__ == "__main__":
    unittest.main()
