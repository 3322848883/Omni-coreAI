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


if __name__ == "__main__":
    unittest.main()
