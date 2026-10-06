# -*- coding: utf-8 -*-
"""提示词必须把 account.position_state 定为权威判据。

背景（实盘 2026-10-02 07:59）：执行器挂入场单后 1-3 秒就把 TP/SL 一起挂上（不等成交），
所以「无持仓 + 有待成交入场单 + 有保护单」是**常态**。AI 看到 protections 有单、
positions 为空，误读成「有持仓可管」→ 发 modify_tp_sl → NO_POSITION，白烧一轮。

修法分两半（两处口径必须一致）：
- snapshot.py：新增 account.position_state / position_state_note（三态标注）
- prompt.py：把 position_state 定为权威判据，并明确 entry_pending 时禁止改保护
"""
from __future__ import annotations

import unittest
from pathlib import Path

from omnialpha.strategist.prompt import SYSTEM_PROMPT
from omnialpha.strategist.snapshot import position_state


class TestPromptDocumentsPositionState(unittest.TestCase):
    def test_field_named_as_authoritative(self):
        self.assertIn("account.position_state", SYSTEM_PROMPT)
        self.assertIn("为准", SYSTEM_PROMPT)

    def test_all_three_states_documented(self):
        for st in ("position_open", "entry_pending", "flat"):
            with self.subTest(state=st):
                self.assertIn(st, SYSTEM_PROMPT)

    def test_entry_pending_forbids_modify(self):
        self.assertIn("entry_pending", SYSTEM_PROMPT)
        self.assertIn("modify_tp_sl", SYSTEM_PROMPT)
        self.assertIn("禁止", SYSTEM_PROMPT)

    def test_points_to_note(self):
        self.assertIn("position_state_note", SYSTEM_PROMPT)


class TestSnapshotStatesMatchPrompt(unittest.TestCase):
    """snapshot 产出的三态必须与提示词里写的完全一致，否则 AI 会对不上号。"""

    def _state(self, account: dict) -> str:
        return position_state(account)[0]

    def test_position_open(self):
        self.assertEqual(self._state({"positions": [{"size": 5}]}), "position_open")

    def test_entry_pending(self):
        self.assertEqual(
            self._state({"positions": [], "open_orders": [{"status": "open"}]}),
            "entry_pending",
        )

    def test_entry_pending_partially_filled(self):
        self.assertEqual(
            self._state({"positions": [], "open_orders": [{"status": "partially_filled"}]}),
            "entry_pending",
        )

    def test_flat(self):
        self.assertEqual(self._state({"positions": [], "open_orders": []}), "flat")

    def test_every_state_has_a_note(self):
        for acc in ({"positions": [{"size": 5}]},
                    {"positions": [], "open_orders": [{"status": "open"}]},
                    {"positions": [], "open_orders": []}):
            with self.subTest(acc=acc):
                st, note = position_state(acc)
                self.assertTrue(note, f"{st} 必须有说明")
                self.assertIn(st, SYSTEM_PROMPT)


class TestPersonaOrphanRuleAgreesWithSnapshot(unittest.TestCase):
    """人格的「孤儿保护单必须撤」条款不能与 snapshot 的 entry_pending 说明打架。

    背景（实盘 2026-10-06）：两条规则原先直接冲突 ——

    - `snapshot.py` 的 entry_pending 说明：「protections 里的单是随入场单预挂的，
      成交后才成为该持仓的保护」（意思是别动）
    - 人格第 25 条：「没有持仓，但存在止盈/止损类挂单 = 孤儿单，**必须撤销**」

    而 `entry_pending` 的定义恰好就是「没有持仓 + 有保护单」。AI 在冲突中选了语气
    更强的人格条款，于是 cycle 216/217 连续两轮 `cancel_price_all,stop_entry_long`
    （相隔 1 分钟），撤了挂、挂了撤。每轮 115,373 prompt tokens。

    修法：人格条款必须自己划清边界——`is_reduce_only: true` 的预挂保护不算孤儿单。
    """

    PERSONA = Path(__file__).resolve().parents[1] / "prompts" / "brooks_btc_pa.md"

    def _clause(self) -> str:
        text = self.PERSONA.read_text(encoding="utf-8")
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "孤儿保护单" in line and "必须" in line:
                # 条款跨行：后续缩进行属于同一条，遇到下一个顶层行才结束
                block = [line]
                for nxt in lines[i + 1:]:
                    if nxt.strip() and not nxt.startswith((" ", "\t")):
                        break
                    block.append(nxt)
                return "\n".join(block)
        self.fail("人格里找不到孤儿保护单条款 —— 条款被删了？")

    def test_clause_excludes_entry_pending(self):
        self.assertIn("entry_pending", self._clause(),
                      "孤儿单条款没排除 entry_pending —— 会把随入场单预挂的保护单撤掉")

    def test_clause_uses_reduce_only_judgement(self):
        self.assertIn("is_reduce_only", self._clause(),
                      "孤儿单条款没用 is_reduce_only 判据，只能靠 text 后缀猜")

    def test_clause_still_forbids_ignoring_real_orphans(self):
        """划边界不等于放宽——真孤儿单仍必须撤。"""
        self.assertIn("禁止", self._clause())


if __name__ == "__main__":
    unittest.main()
