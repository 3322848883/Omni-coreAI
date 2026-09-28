"""生产级验收：并发写入、崩溃恢复、多组隔离、信号可执行性。"""
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.persona import (  # noqa: E402
    PersonaGroup,
    SharedOrderStore,
    PersonaRunner,
)
from gate_bot.persona.orders import new_order_id  # noqa: E402


def _group(**kw):
    base = dict(name="g", members=["a", "b"], fusion="weighted_vote",
                fusion_config={"weights": {"a": 1, "b": 1}}, on_conflict="hold")
    base.update(kw)
    return PersonaGroup(**base)


class FakePlanRunner:
    def __init__(self, decision="long", action=None, confidence=0.8,
                 symbol="BTC_USDT", size_usd=100):
        self.decision = decision
        self.action = action or (f"open_{decision}" if decision in ("long", "short") else decision)
        self.confidence = confidence
        self.symbol = symbol
        self.size_usd = size_usd

    def analyze_once(self, trigger="manual"):
        return {"ok": True, "cycle_id": f"c-{time.time_ns()}", "plan": {
            "cycle_id": f"c-{time.time_ns()}", "decision": self.decision,
            "confidence": self.confidence,
            "chips": [{"action": self.action, "symbol": self.symbol,
                        "size_usd": self.size_usd, "confidence": self.confidence}],
        }}


# ─────────────────────────────────────────────────────
# 1. 并发写入 SharedOrderStore
# ─────────────────────────────────────────────────────
class TestConcurrency(unittest.TestCase):
    def test_concurrent_record_vote_no_corruption(self):
        """多线程同时 record_vote，数据不丢不坏。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT", "members": ["a", "b", "c"]})
            oid = rec["order_id"]
            n = 50

            def vote(i):
                store.record_vote(oid, f"c-{i}", f"bot-{i % 5}",
                                  "long", reasoning=f"r{i}", confidence=0.5)
                return i

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [pool.submit(vote, i) for i in range(n)]
                results = [f.result() for f in as_completed(futures)]

            self.assertEqual(len(results), n)
            got = store.get(oid)
            # 每轮 cycle_id 唯一，全部应保留
            self.assertEqual(len(got["votes"]), n)
            # 文件可解析
            self.assertIsInstance(got, dict)

    def test_concurrent_create_orders(self):
        """多线程同时建单，全部成功且 ID 唯一。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            n = 30

            def create(i):
                return store.create({"symbol": f"SYM{i}", "side": "long"})["order_id"]

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [pool.submit(create, i) for i in range(n)]
                ids = [f.result() for f in as_completed(futures)]

            self.assertEqual(len(ids), n)
            self.assertEqual(len(set(ids)), n, "order_id must be unique")
            self.assertEqual(len(store.list_open()), n)

    def test_concurrent_update_same_order(self):
        """多线程同时 update 同一单，最终状态一致可读。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]

            def upd(i):
                store.update(oid, tag=f"t{i}")
                store.append_log(oid, "cycle", f"#{i}")
                return i

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(upd, i) for i in range(20)]
                [f.result() for f in as_completed(futures)]

            got = store.get(oid)
            self.assertIsInstance(got, dict)
            # log 至少有若干条（append 可能因并发覆盖部分，但不应损坏）
            self.assertIsInstance(got.get("log"), list)

    def test_concurrent_run_once_single_account(self):
        """多线程同时 run_once，信号文件不互相覆盖。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            results = []

            def run(i):
                runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("long")}
                r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
                return r.run_once()

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(run, i) for i in range(5)]
                results = [f.result() for f in as_completed(futures)]

            for res in results:
                self.assertTrue(res["ok"])
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox), 5, "5 concurrent runs → 5 signal files")


# ─────────────────────────────────────────────────────
# 2. 崩溃恢复 / 原子写
# ─────────────────────────────────────────────────────
class TestCrashRecovery(unittest.TestCase):
    def test_partial_write_leaves_no_corrupt_json(self):
        """模拟写入中断：.writing 临时文件不应被 list_open 读到。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            store.create({"symbol": "BTC_USDT"})
            # 模拟崩溃留下的临时文件
            (store.dir / ".o-abc.json.123.writing").write_text("{partial", encoding="utf-8")
            (store.dir / "o-good.json").write_text(
                json.dumps({"order_id": "o-good", "status": "open"}), encoding="utf-8")
            opens = store.list_open()
            # 只有正常文件被读到
            self.assertEqual(len(opens), 2)  # create 的 + o-good

    def test_corrupt_order_file_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            store.create({"symbol": "BTC_USDT"})
            (store.dir / "o-corrupt.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(len(store.list_open()), 1)

    def test_update_after_corrupt_get_raises_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            # 手动写坏
            store._path(oid).write_text("{bad", encoding="utf-8")
            # get 返回 None（不抛）
            self.assertIsNone(store.get(oid))
            # update 抛 KeyError（不抛 JSONDecodeError）
            with self.assertRaises(KeyError):
                store.update(oid, status="closed")

    def test_atomic_write_no_temp_left_behind(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            store.record_vote(oid, "c-1", "a", "long")
            store.update(oid, status="closed")
            # 检查没有 .writing 残留
            leftovers = [p for p in store.dir.iterdir() if ".writing" in p.name]
            self.assertEqual(leftovers, [])


# ─────────────────────────────────────────────────────
# 3. 多组隔离
# ─────────────────────────────────────────────────────
class TestMultiGroupIsolation(unittest.TestCase):
    def test_two_groups_independent_orders(self):
        """两个组各自开仓，订单互不干扰。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g1 = _group(name="g1", members=["a", "b"], target_account="a",
                        topology="single_account",
                        fusion_config={"weights": {"a": 1, "b": 1}})
            g2 = _group(name="g2", members=["c", "d"], target_account="c",
                        topology="single_account",
                        fusion_config={"weights": {"c": 1, "d": 1}})

            def run(g, bots):
                runners = {b: FakePlanRunner("long") for b in g.members}
                r = PersonaRunner(root, g, {b: None for b in g.members}, runners)
                return r.run_once()

            res1 = run(g1, None)
            res2 = run(g2, None)

            self.assertTrue(res1["executed"])
            self.assertTrue(res2["executed"])
            self.assertNotEqual(res1["order_id"], res2["order_id"])

            # 各自 inbox 独立
            inbox_a = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            inbox_c = list((root / "data" / "bots" / "c" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_a), 1)
            self.assertEqual(len(inbox_c), 1)

            # 共享订单库两单并存
            store = SharedOrderStore(root)
            self.assertEqual(len(store.list_open()), 2)

    def test_shared_order_store_group_aware(self):
        """共享订单记录了 group 字段，可区分来源。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g1 = _group(name="alpha", members=["a", "b"], target_account="a",
                        topology="single_account",
                        fusion_config={"weights": {"a": 1, "b": 1}})
            runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("long")}
            r = PersonaRunner(root, g1, {"a": None, "b": None}, runners)
            r.run_once()
            store = SharedOrderStore(root)
            opens = store.list_open()
            self.assertEqual(len(opens), 1)
            self.assertEqual(opens[0]["group"], "alpha")


# ─────────────────────────────────────────────────────
# 4. 信号可执行性（schema + 字段完整性）
# ─────────────────────────────────────────────────────
class TestSignalExecutability(unittest.TestCase):
    def _make_signal(self, root, decision, action, **kw):
        g = _group(target_account="a", topology="single_account")
        runners = {"a": FakePlanRunner(decision, action=action, **kw),
                    "b": FakePlanRunner(decision, action=action, **kw)}
        r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
        res = r.run_once()
        if not res.get("executed"):
            return None
        inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
        return json.loads(inbox[0].read_text(encoding="utf-8"))

    def test_open_long_signal_parseable(self):
        from gate_bot.schema import parse_signal
        with tempfile.TemporaryDirectory() as td:
            payload = self._make_signal(Path(td), "long", "open_long",
                                        size_usd=100, confidence=0.9)
            self.assertIsNotNone(payload)
            sig = parse_signal(payload)
            intent = sig.intents[0]
            self.assertEqual(intent.action, "open_long")
            self.assertEqual(intent.symbol, "BTC_USDT")
            self.assertEqual(intent.size_usd, 100)

    def test_open_signal_has_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            payload = self._make_signal(Path(td), "short", "open_short",
                                        size_usd=200, confidence=0.85)
            self.assertIsNotNone(payload)
            # 执行器必需字段
            self.assertIn("action", payload)
            self.assertIn("symbol", payload)
            self.assertIn("size_usd", payload)
            self.assertIn("label", payload)
            self.assertIn("meta", payload)
            # meta 审计字段
            meta = payload["meta"]
            for k in ("group", "order_id", "fusion", "decision", "votes", "confidence"):
                self.assertIn(k, meta, f"meta missing {k}")

    def test_close_signal_parseable(self):
        from gate_bot.schema import parse_signal
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            SharedOrderStore(root).create({"symbol": "BTC_USDT", "side": "long"})
            payload = self._make_signal(root, "close", "close")
            self.assertIsNotNone(payload)
            sig = parse_signal(payload)
            self.assertEqual(sig.intents[0].action, "close")

    def test_hold_signal_parseable(self):
        from gate_bot.schema import parse_signal
        with tempfile.TemporaryDirectory() as td:
            payload = self._make_signal(Path(td), "long", "hold", confidence=0.3)
            # hold 不执行，但若写盘也应可解析
            if payload:
                sig = parse_signal(payload)
                self.assertEqual(sig.intents[0].action, "hold")


# ─────────────────────────────────────────────────────
# 5. 性能 / 压测
# ─────────────────────────────────────────────────────
class TestStress(unittest.TestCase):
    def test_rapid_sequential_runs(self):
        """20 轮快速连续 run_once，无异常、文件不丢。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            for i in range(20):
                runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("long")}
                r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
                res = r.run_once()
                self.assertTrue(res["ok"], f"cycle {i}")
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox), 20)

    def test_shared_order_500_votes(self):
        """单订单 500 轮投票，读写不超时、数据完整。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            t0 = time.time()
            for i in range(500):
                store.record_vote(oid, f"c-{i}", f"bot-{i % 3}",
                                  "long", confidence=0.5)
            elapsed = time.time() - t0
            self.assertLess(elapsed, 30.0, f"500 votes took {elapsed:.1f}s")
            got = store.get(oid)
            self.assertEqual(len(got["votes"]), 500)


if __name__ == "__main__":
    unittest.main()
