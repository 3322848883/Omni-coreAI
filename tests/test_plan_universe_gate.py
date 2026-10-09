# -*- coding: utf-8 -*-
"""plan 层宇宙闸门 + 按币名额（T5-b / T8，见 docs/compose/spec/symbol-as-parameter.md）。

证据 B-5：plan 层**没有宇宙闸门**（`risk.py:51`、`schema.py:59-65` 只校验字符集）——
宇宙外的 chip 静默进 inbox，靠 executor 白名单兜底 → 白烧一轮。
证据 B-4：`max_chips` 跨币抢名额 → 一个币占满，其他币系统性出局。
"""
from __future__ import annotations

import unittest

from omnialpha.strategist.risk import RiskConfig, apply_risk
from omnialpha.strategist.schema import PlanError, _safe_symbol, parse_plan, parse_plan_text


def _chip(symbol, action="open_long", conf=0.9, **over) -> dict:
    base = {"symbol": symbol, "action": action, "confidence": conf, "size_usd": 50}
    base.update(over)
    return base


def _plan(chips: list[dict]):
    return parse_plan({"cycle_id": "c1", "chips": chips})


class TestUniverseGateInRisk(unittest.TestCase):
    def test_out_of_universe_rejected_with_readable_note(self):
        plan = _plan([_chip("BTC_USDT"), _chip("ETH_USDT")])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, symbols=["ETH_USDT"]))
        self.assertEqual([c.symbol for c in r.accepted], ["ETH_USDT"])
        self.assertEqual([c.symbol for c in r.rejected], ["BTC_USDT"])
        note = next(n for n in r.notes if "BTC_USDT" in n)
        self.assertIn("universe", note)
        self.assertIn("ETH_USDT", note, "拒绝文案必须带可选列表，模型才能一次改对")

    def test_missing_symbols_means_no_check(self):
        """缺省 `symbols=None` = 不校验 —— 旧调用方（脚本/测试）行为不变。"""
        plan = _plan([_chip("BTC_USDT")])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5))
        self.assertEqual(len(r.accepted), 1)
        self.assertFalse(r.rejected)

    def test_empty_universe_rejects_everything(self):
        """`symbols: []` 是「未配置」，不是「不限制」—— 必须拒绝而不是放开。"""
        plan = _plan([_chip("BTC_USDT")])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, symbols=[]))
        self.assertFalse(r.accepted)
        self.assertEqual(len(r.rejected), 1)

    def test_gate_runs_before_quota(self):
        """宇宙外的 chip 不该抢走名额（先闸门、后配额）。"""
        plan = _plan([_chip("BTC_USDT", conf=0.95), _chip("ETH_USDT", conf=0.9)])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, max_chips=1, symbols=["ETH_USDT"]))
        self.assertEqual([c.symbol for c in r.accepted], ["ETH_USDT"])

    def test_manage_actions_also_gated(self):
        """`cancel_all` 是钝动作 —— 宇宙外同样必须被拒（规则 19 的程序化落地）。"""
        plan = _plan([_chip("BTC_USDT", action="cancel_all", conf=0.9)])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, symbols=["ETH_USDT"]))
        self.assertFalse(r.accepted)
        self.assertTrue(r.rejected)

    def test_symbol_case_and_space_normalised(self):
        plan = _plan([_chip("btc_usdt")])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, symbols=[" BTC_USDT "]))
        self.assertEqual([c.symbol for c in r.accepted], ["BTC_USDT"])


class TestUniverseGateInSchema(unittest.TestCase):
    def test_safe_symbol_checks_membership(self):
        self.assertEqual(_safe_symbol("BTC_USDT", ["BTC_USDT", "ETH_USDT"]), "BTC_USDT")
        with self.assertRaises(PlanError) as cm:
            _safe_symbol("BTC_USDT", ["ETH_USDT"])
        self.assertIn("ETH_USDT", str(cm.exception), "错误里要给出可选列表")

    def test_safe_symbol_without_universe_unchanged(self):
        self.assertEqual(_safe_symbol("BTC_USDT"), "BTC_USDT")

    def test_parse_plan_accepts_universe(self):
        plan = parse_plan({"chips": [_chip("ETH_USDT")]}, ["ETH_USDT"])
        self.assertEqual(plan.chips[0].symbol, "ETH_USDT")

    def test_parse_plan_rejects_out_of_universe(self):
        with self.assertRaises(PlanError) as cm:
            parse_plan({"chips": [_chip("BTC_USDT")]}, ["ETH_USDT"])
        self.assertIn("universe", str(cm.exception))

    def test_parse_plan_text_accepts_universe(self):
        text = '{"cycle_id":"c","chips":[{"symbol":"ETH_USDT","action":"hold"}]}'
        self.assertEqual(parse_plan_text(text, ["ETH_USDT"]).chips[0].symbol, "ETH_USDT")
        with self.assertRaises(PlanError):
            parse_plan_text(text, ["BTC_USDT"])

    def test_no_universe_keeps_old_behaviour(self):
        plan = parse_plan({"chips": [_chip("BTC_USDT")]})
        self.assertEqual(plan.chips[0].symbol, "BTC_USDT")


class TestMaxChipsPerSymbol(unittest.TestCase):
    """一币多 chip 不再挤掉其他币（B-4）。"""

    TWO_EACH = [
        _chip("BTC_USDT", conf=0.95), _chip("BTC_USDT", conf=0.90),
        _chip("ETH_USDT", conf=0.85), _chip("ETH_USDT", conf=0.80),
    ]

    def test_each_symbol_gets_its_own_quota(self):
        r = apply_risk(_plan(self.TWO_EACH),
                       RiskConfig(min_confidence=0.5, max_chips=5,
                                  symbols=["BTC_USDT", "ETH_USDT"]))
        syms = sorted(c.symbol for c in r.accepted)
        self.assertEqual(syms, ["BTC_USDT", "ETH_USDT"],
                         "按币配额后每个币都应留下方案")
        self.assertTrue(any("max_chips_per_symbol" in n for n in r.notes),
                        f"截断必须留痕：{r.notes}")

    def test_keeps_highest_confidence_within_symbol(self):
        r = apply_risk(_plan(self.TWO_EACH),
                       RiskConfig(min_confidence=0.5, max_chips=5,
                                  symbols=["BTC_USDT", "ETH_USDT"]))
        kept = {c.symbol: c.confidence for c in r.accepted}
        self.assertEqual(kept["BTC_USDT"], 0.95)
        self.assertEqual(kept["ETH_USDT"], 0.85)

    def test_global_max_chips_still_caps_total(self):
        r = apply_risk(_plan(self.TWO_EACH),
                       RiskConfig(min_confidence=0.5, max_chips=1,
                                  symbols=["BTC_USDT", "ETH_USDT"]))
        self.assertEqual(len(r.accepted), 1)
        self.assertEqual(r.accepted[0].symbol, "BTC_USDT", "总上限仍按置信度取")
        self.assertTrue(any("max_chips" in n for n in r.notes))

    def test_explicit_per_symbol_quota(self):
        r = apply_risk(_plan(self.TWO_EACH),
                       RiskConfig(min_confidence=0.5, max_chips=5, max_chips_per_symbol=2,
                                  symbols=["BTC_USDT", "ETH_USDT"]))
        self.assertEqual(len(r.accepted), 4, "配额 2 时两个币各留 2 条")

    def test_single_symbol_plan_behaviour_unchanged(self):
        """I11：单币路径逐字不变 —— 缺省 `max_chips_per_symbol=1` 不得砍单币方案。

        `eth-range-paper`(max_chips=2) / `brooks-pa-paper`(max_chips=2) 都是单币 bot。
        """
        plan = _plan([_chip("BTC_USDT", conf=0.95), _chip("BTC_USDT", conf=0.90)])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, max_chips=2, symbols=["BTC_USDT"]))
        self.assertEqual(len(r.accepted), 2)
        self.assertFalse(r.rejected)

    def test_manage_actions_do_not_consume_symbol_quota(self):
        """撤单是清理动作，不占名额（老口径不许被按币配额磨平）。"""
        plan = _plan([
            _chip("BTC_USDT", action="cancel_all", conf=0.95),
            _chip("BTC_USDT", action="open_long", conf=0.62),
            _chip("ETH_USDT", action="cancel_price_all", conf=0.9),
        ])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, max_chips=5,
                                        symbols=["BTC_USDT", "ETH_USDT"]))
        self.assertEqual(len(r.accepted), 3)
        self.assertFalse(r.rejected)

    def test_zero_max_chips_means_unlimited(self):
        """`max_chips: 0` 是既有语义（douglas-paper）= 不限名额，不能被按币配额改成 1。"""
        plan = _plan([_chip("BTC_USDT", conf=0.95), _chip("BTC_USDT", conf=0.90)])
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, max_chips=0, symbols=["BTC_USDT"]))
        self.assertEqual(len(r.accepted), 2)


class TestLiveWiring(unittest.TestCase):
    """闸门必须接在**生产路径**上。

    没有这条，`apply_risk` 的闸门在生产里永远 `symbols=None`（不校验）——
    测试全绿但功能没生效，正是本仓反复出现的「看起来生效、实际不生效」。
    """

    def test_build_plan_runner_wires_universe(self):
        from pathlib import Path as _P

        from omnialpha.__main__ import _build_plan_runner
        from omnialpha.config import load_bot_config
        from omnialpha.watcher import ProjectPaths

        root = _P(__file__).resolve().parents[1]
        paths = ProjectPaths(root)
        bot = load_bot_config(paths.config_dir / "ab-multi-cur.yaml")
        runner = _build_plan_runner(bot, paths)
        self.assertEqual(list(runner.cfg.risk.symbols), list(bot.symbols),
                         "生产路径必须把宇宙传进 RiskConfig，否则闸门是死的")
        self.assertEqual(runner.cfg.risk.max_chips_per_symbol, 1)

    def test_unrestricted_bot_is_not_gated(self):
        """`symbols: []` + `symbols_unrestricted: true` 的 bot → None = 不校验。

        放开品种不该被闸门变成「一律拒绝」（那是把"不限制"读成了"什么都不许"）。
        """
        from dataclasses import replace
        from pathlib import Path as _P

        from omnialpha.__main__ import _build_plan_runner
        from omnialpha.config import load_bot_config
        from omnialpha.watcher import ProjectPaths

        root = _P(__file__).resolve().parents[1]
        paths = ProjectPaths(root)
        bot = load_bot_config(paths.config_dir / "ab-multi-cur.yaml")
        bot = replace(bot, symbols=[], symbols_unrestricted=True)
        runner = _build_plan_runner(bot, paths)
        self.assertIsNone(runner.cfg.risk.symbols)


if __name__ == "__main__":
    unittest.main()
