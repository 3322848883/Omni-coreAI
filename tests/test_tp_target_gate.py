# -*- coding: utf-8 -*-
"""`meta.tp_target` 闸门（`PlanRunner._gate_tp_target`）。

背景（2026-10-26 实测 martin-grid-paper 连跑 6 轮）：
人格第十节第 6 条明写「缺任何一档的 `meta.tp_target` = 本轮自检失败，回去补齐」，
但 r5/r6 各带 7/7 档、**r7 一条都没带（0/7）** —— 软约束覆盖率不是 100%。

缺它的后果不是崩：`ensure_grid_tp` 对没有 target 的档会 skip 并留痕「交回 AI」，
于是 7 个网格档**全部没有逐档止盈**，只能等马丁共用 TP 把全仓平掉 = 假网格。

闸门刻意保守：只在「带 tp 的入场腿 ≥3 条、且一条 tp_target 都没有」时才报，
单个/两个档可能是马丁腿或正常「交回 AI」，不该误报。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.strategist.loop import PlanRunner, StrategistConfig


def _runner(td: str, *, require: bool = False) -> PlanRunner:
    cfg = StrategistConfig(
        symbols=["ETH_USDT"], vision=False, prompt_file="",
        bot_root=Path(td), bot_id="tpg", env="paper", write_hold=True,
    )
    if require:
        cfg.require_tp_target = True
    return PlanRunner(client=object(), cfg=cfg, inbox=Path(td) / "inbox",
                      history_dir=Path(td) / "state", llm=object())


def _plan(legs: list[dict]):
    """legs: [{action, tp, tp_target?}] → 最小 Plan 对象（用真 schema 解析，别造假结构）"""
    from omnialpha.strategist.schema import parse_plan_text
    import json
    chips = []
    for lg in legs:
        c = {"symbol": "ETH_USDT", "action": lg["action"], "confidence": 0.8,
             "price": 2300.0, "size_usd": 23.0, "sl": 2168.0}
        if lg.get("tp") is not None:
            c["tp"] = lg["tp"]
        if lg.get("tp_target") is not None:
            c["meta"] = {"tp_target": lg["tp_target"]}
        chips.append(c)
    return parse_plan_text(json.dumps({"chips": chips}), symbols=["ETH_USDT"])


def _grid(n=7, with_target=False, tp=2399.3):
    return [{"action": "open_long", "tp": tp,
             "tp_target": 2310.0 if with_target else None} for _ in range(n)]


class TestTpTargetGate(unittest.TestCase):
    def test_all_missing_warns(self):
        """真实现场 r7：7 条网格腿全缺 tp_target。"""
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)
            p = _plan(_grid(7))
            notes = r._gate_tp_target(p)
            self.assertEqual(len(notes), 1, notes)
            self.assertIn("tp_target_all_missing", notes[0])
            self.assertEqual(len(p.chips), 7, "默认只告警，不丢腿")

    def test_present_is_clean(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)
            p = _plan(_grid(7, with_target=True))
            self.assertEqual(r._gate_tp_target(p), [])

    def test_partial_still_clean(self):
        """只要有一条带了就不报（保守：可能只是马丁腿没带）。"""
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)
            legs = _grid(3, with_target=True) + _grid(4)
            p = _plan(legs)
            self.assertEqual(r._gate_tp_target(p), [])

    def test_few_legs_not_reported(self):
        """<3 条带 tp 的 leg 时不报 —— 那是马丁腿或「交回 AI」的正常形态。"""
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)
            p = _plan(_grid(2))
            self.assertEqual(r._gate_tp_target(p), [])

    def test_legs_without_tp_ignored(self):
        """`modify_tp_sl` / 无 tp 的腿不计入分母。"""
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)
            legs = _grid(3) + [{"action": "modify_tp_sl", "tp": None}] * 4
            p = _plan(legs)
            notes = r._gate_tp_target(p)
            self.assertEqual(len(notes), 1)
            self.assertIn("3/3", notes[0], notes[0])

    def test_require_mode_drops_legs(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, require=True)
            p = _plan(_grid(7))
            notes = r._gate_tp_target(p)
            self.assertIn("require_tp_target", notes[0])
            self.assertEqual(len(p.chips), 0, "硬闸门开启时应丢光这些腿")

    def test_require_mode_keeps_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, require=True)
            p = _plan(_grid(7, with_target=True))
            self.assertEqual(r._gate_tp_target(p), [])
            self.assertEqual(len(p.chips), 7)


if __name__ == "__main__":
    unittest.main()
