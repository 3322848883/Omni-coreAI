"""执行回执通道：执行侧主动回传结果，产出侧直接读。

回归背景（2026-10-05 架构盘点 S23）：persona 下单后靠**异步扫目标账户的
`trades.jsonl` 尾 500 行**来猜「这笔单成交了没、赚了多少」—— 取不到就跳过
记账（实测漏记 458/459 笔）。进程间没有回执通道。

修法：`run` 执行成功后把结果写成 `data/shared/receipts/<key>.json`；
persona 侧优先读回执，读不到再回退旧路径。
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
from omnialpha.watcher import receipt_path, write_receipt  # noqa: E402


class TestReceiptPath(unittest.TestCase):
    def test_sanitizes_key(self):
        p = receipt_path(Path("/r"), "o-1|c/2 ../evil")
        self.assertNotIn("/", p.name.replace("receipts", ""))
        self.assertTrue(p.name.endswith(".json"))

    def test_empty_key_falls_back(self):
        self.assertEqual(receipt_path(Path("/r"), "").name, "unknown.json")

    def test_write_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_receipt(root, "o-1|c-1", {"order_id": "o-1", "ts": 5, "ok": True})
            p = receipt_path(root, "o-1|c-1")
            self.assertTrue(p.exists())
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["order_id"], "o-1")
            leftovers = list(p.parent.glob(".*writing"))
            self.assertEqual(leftovers, [], "不应留下 .writing 临时文件")


class TestPersonaReadsReceipt(unittest.TestCase):
    def _runner(self, root):
        group = PersonaGroup(name="g", members=["a", "b"], target_account="a",
                             fusion="weighted_vote", on_conflict="hold")
        return PersonaRunner(root, group, {}, {})

    def test_receipt_preferred_over_trades_jsonl(self):
        """回执存在时，直接用它（不必扫 trades.jsonl）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_receipt(root, "o-9|c-1", {
                "order_id": "o-9", "ts": 100, "ok": True,
                "realized_pnl": 12.5, "entry_price": 2702.0,
            })
            r = self._runner(root)
            facts = r._exec_facts_of("o-9", "a")
            self.assertEqual(facts["pnl"], 12.5)
            self.assertEqual(facts["entry_price"], 2702.0)

    def test_newest_receipt_wins(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_receipt(root, "o-9|c-1", {"order_id": "o-9", "ts": 100,
                                            "ok": True, "realized_pnl": 1.0})
            write_receipt(root, "o-9|c-2", {"order_id": "o-9", "ts": 200,
                                            "ok": True, "realized_pnl": 9.0})
            r = self._runner(root)
            self.assertEqual(r._exec_facts_of("o-9", "a")["pnl"], 9.0)

    def test_other_order_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_receipt(root, "o-other|c-1", {"order_id": "o-other", "ts": 100,
                                                "ok": True, "realized_pnl": 5.0})
            r = self._runner(root)
            facts = r._exec_facts_of("o-9", "a")
            self.assertIsNone(facts["pnl"], "不该拿别的单的回执")

    def test_falls_back_when_no_receipt(self):
        """没有回执时回退旧路径（不报错、返回 None 而不是 0）。"""
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td))
            facts = r._exec_facts_of("o-none", "a")
            self.assertIsNone(facts["pnl"])
            self.assertIsNone(facts["entry_price"])


if __name__ == "__main__":
    unittest.main()
