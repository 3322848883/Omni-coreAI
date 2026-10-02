# -*- coding: utf-8 -*-
"""persona 模式的 `modify_tp_sl` 回归测试。

背景：`PersonaRunner._normalize_action()` 原来把 `modify_tp_sl` 降级成 `hold`，
注释写「schema 暂不支持改单」—— 但 **`modify_tp_sl` 一直是 schema 合法动作**
（`schema.ACTIONS` 里有，`executor.execute()` 会分发给 `_modify_tp_sl`）。
后果是**人格想调整止盈止损时静默什么都不做**，而且日志上看不出被丢弃。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.persona.config import PersonaGroup  # noqa: E402
from omnialpha.persona.runner import PersonaRunner  # noqa: E402
from omnialpha.schema import ACTIONS  # noqa: E402


class TestNormalizeAction(unittest.TestCase):
    def test_modify_tp_sl_passes_through(self):
        """核心回归：不再降级成 hold。"""
        self.assertEqual(PersonaRunner._normalize_action("modify_tp_sl"), ("modify_tp_sl", None))
        self.assertEqual(PersonaRunner._normalize_action("MODIFY_TP_SL"), ("modify_tp_sl", None))

    def test_modify_aliases(self):
        self.assertEqual(PersonaRunner._normalize_action("modify"), ("modify_tp_sl", None))
        self.assertEqual(PersonaRunner._normalize_action("modify_tp"), ("modify_tp_sl", None))

    def test_close_variants_still_split_side(self):
        self.assertEqual(PersonaRunner._normalize_action("close_long"), ("close", "long"))
        self.assertEqual(PersonaRunner._normalize_action("close_short"), ("close", "short"))

    def test_other_actions_untouched(self):
        self.assertEqual(PersonaRunner._normalize_action("open_long"), ("open_long", None))
        self.assertEqual(PersonaRunner._normalize_action("hold"), ("hold", None))

    def test_modify_tp_sl_is_a_schema_action(self):
        """证明旧注释「schema 暂不支持改单」是错的。"""
        self.assertIn("modify_tp_sl", ACTIONS)


class TestExecuteWritesModify(unittest.TestCase):
    def _group(self, topology="single_account"):
        return PersonaGroup(
            name="g", members=["a", "b"], topology=topology,
            target_account="a", fusion="weighted_vote",
            fusion_config={"weights": {"a": 1, "b": 1}},
        )

    def _run(self, action, chip_extra):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = PersonaRunner(root, self._group(), {"a": None, "b": None}, {})
            fusion = {"decision": "long", "action": action, "mode": "weighted_vote",
                      "votes": {"a": "long", "b": "long"}, "reason": "t", "confidence": 0.9}
            chip = {"action": action, "symbol": "BTC_USDT", "size_usd": 100}
            chip.update(chip_extra)
            r._execute(fusion, {"a": {"chips": [chip]}}, None)
            files = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertTrue(files, "应写出一个信号文件")
            return json.loads(files[0].read_text(encoding="utf-8"))

    def test_modify_tp_sl_reaches_inbox(self):
        """人格要改保护 → 写出的信号必须是 modify_tp_sl（而不是 hold）。"""
        payload = self._run("modify_tp_sl", {"tp": 87000.0, "sl": 85000.0})
        self.assertEqual(payload.get("action"), "modify_tp_sl")
        self.assertEqual(payload.get("tp"), 87000.0)
        self.assertEqual(payload.get("sl"), 85000.0)

    def test_modify_without_prices_falls_back_to_hold(self):
        """两个目标价都没有 → 退回 hold（否则 executor 必然报 requires tp and/or sl）。"""
        payload = self._run("modify_tp_sl", {})
        self.assertEqual(payload.get("action"), "hold")

    def test_modify_with_only_sl_is_kept(self):
        payload = self._run("modify_tp_sl", {"sl": 84500.0})
        self.assertEqual(payload.get("action"), "modify_tp_sl")


if __name__ == "__main__":
    unittest.main()
