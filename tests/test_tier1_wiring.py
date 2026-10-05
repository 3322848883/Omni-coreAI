"""契约 Tier 1 的接线：journal 与订单上下文（docs/compose/spec/pa-skills-upgrade.md [S2]）。

T4 的验收：跑一轮后 journal 含 region/rule_ids；订单上下文出现**模型声明的失效价**。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.memory import MemoryJournal  # noqa: E402
from omnialpha.memory.context import _format_order_context  # noqa: E402
from omnialpha.persona.orders import SharedOrderStore  # noqa: E402


def _journal_rec(root: Path, bot: str = "b") -> dict:
    p = root / "data" / "bots" / bot / "state" / "memory_journal.jsonl"
    return json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])


class TestJournalTier1Fields(unittest.TestCase):
    def test_extra_fields_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            MemoryJournal(root, "b").append(
                cycle_id="c1", decision="long", reasoning="r",
                region="trend", rule_ids=["SB-06"], risk_pct=1.4,
                invalidation_price=85560.0,
            )
            rec = _journal_rec(root)
            self.assertEqual(rec["region"], "trend")
            self.assertEqual(rec["rule_ids"], ["SB-06"])
            self.assertEqual(rec["risk_pct"], 1.4)
            self.assertEqual(rec["invalidation_price"], 85560.0)

    def test_absent_fields_do_not_add_keys(self):
        """没填就不写键 —— 老 journal 的形态不变，下游按 .get() 读不受影响。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            MemoryJournal(root, "b").append(cycle_id="c1", decision="hold")
            rec = _journal_rec(root)
            for k in ("region", "rule_ids", "risk_pct", "invalidation_price",
                      "time_stop_bars", "give_back_pct"):
                self.assertNotIn(k, rec)


class TestOrderPremiseInvalidation(unittest.TestCase):
    @staticmethod
    def _mk_order(store: SharedOrderStore) -> dict:
        return store.create({
            "order_id": "o-1", "symbol": "BTC_USDT", "group": "g",
            "topology": "single_account", "target_account": "b",
            "members": ["b"], "side": "long", "status": "open",
        })

    def test_set_and_read_back(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            self._mk_order(store)
            store.set_premise_invalidation("o-1", 85560.0, note="trend")
            ctx = store.get_order_context("o-1")
            self.assertEqual(ctx["premise_invalidation"]["price"], 85560.0)
            self.assertEqual(ctx["premise_invalidation"]["note"], "trend")

    def test_absent_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            self._mk_order(store)
            self.assertIsNone(store.get_order_context("o-1")["premise_invalidation"])

    def test_model_declared_does_not_mix_with_field_diff(self):
        """模型声明的前提失效价与引擎的字段变更 diff 分开存。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            self._mk_order(store)
            store.add_invalidation("o-1", "sl", "2694.0", "2688.0")
            store.set_premise_invalidation("o-1", 85560.0)
            ctx = store.get_order_context("o-1")
            self.assertEqual(len(ctx["invalidation"]), 1)          # 字段 diff
            self.assertEqual(ctx["premise_invalidation"]["price"], 85560.0)


class TestFormatOrderContext(unittest.TestCase):
    def test_premise_invalidation_printed_as_value(self):
        """必须给值而不是计数 —— 只报「N 条」等于没记。"""
        txt = _format_order_context({
            "order_id": "o-1", "symbol": "BTC_USDT", "side": "long",
            "premise_invalidation": {"price": 85560.0, "note": "trend"},
        })
        self.assertIn("85560.0", txt)
        self.assertIn("前提失效", txt)

    def test_no_premise_no_line(self):
        txt = _format_order_context({"order_id": "o-1", "symbol": "BTC_USDT", "side": "long"})
        self.assertNotIn("前提失效", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
