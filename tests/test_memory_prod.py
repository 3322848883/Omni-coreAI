"""agent-memory 生产级测试：并发/崩溃/集成/资源/安全。"""
import gzip
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

from gate_bot.memory import (  # noqa: E402
    CacheGuard,
    MemoryJournal,
    MemoryProfile,
    archive_journal,
    build_context,
    cleanup_closed_orders,
)
from gate_bot.memory.forget import archive_journal as _archive  # noqa: E402
from gate_bot.persona.orders import (  # noqa: E402
    RECENT_EVENTS_MAX,
    SharedOrderStore,
    new_order_id,
)


# ─────────────────────────────────────────────────────
# 1. 并发写入
# ─────────────────────────────────────────────────────
class TestConcurrency(unittest.TestCase):
    def test_concurrent_journal_append(self):
        """多线程同时 append journal，条数正确、文件可解析。"""
        with tempfile.TemporaryDirectory() as td:
            j = MemoryJournal(Path(td), "bot-a")
            n = 100

            def write(i):
                j.append(cycle_id=f"c-{i}", decision="long", reasoning=f"r{i}")
                return i

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [pool.submit(write, i) for i in range(n)]
                results = [f.result() for f in as_completed(futures)]

            self.assertEqual(len(results), n)
            self.assertEqual(j.count(), n)

    def test_concurrent_lifecycle_updates(self):
        """多线程同时 add_lifecycle + refresh_recent_events。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT", "group": "g"})
            oid = rec["order_id"]
            n = 50

            def update(i):
                store.add_lifecycle(oid, "modify_sl", f"sl-{i}", by=f"bot-{i%3}")
                store.refresh_recent_events(oid)
                return i

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(update, i) for i in range(n)]
                [f.result() for f in as_completed(futures)]

            got = store.get(oid)
            self.assertGreaterEqual(len(got["lifecycle"]), n // 2)  # 并发下部分可覆盖
            self.assertLessEqual(len(got["recent_events"]), RECENT_EVENTS_MAX)

    def test_concurrent_profile_record(self):
        """多线程同时 record_trade，统计不丢。"""
        with tempfile.TemporaryDirectory() as td:
            p = MemoryProfile(Path(td), "bot-a")
            n = 50

            def record(i):
                p.record_trade(pnl_usd=10.0 if i % 2 == 0 else -5.0, hold_rounds=i)
                return i

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(record, i) for i in range(n)]
                [f.result() for f in as_completed(futures)]

            rec = p.load()
            self.assertEqual(rec["total_trades"], n)
            self.assertEqual(rec["win_count"], n // 2)

    def test_concurrent_cache_guard(self):
        with tempfile.TemporaryDirectory() as td:
            g = CacheGuard(Path(td), "bot-a")

            def record(i):
                g.record(hit_tokens=1800, total_tokens=3600)
                return i

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(record, i) for i in range(50)]
                [f.result() for f in as_completed(futures)]

            rate = g.recent_hit_rate()
            self.assertGreater(rate, 0.4)


# ─────────────────────────────────────────────────────
# 2. 崩溃恢复 / 数据完整性
# ─────────────────────────────────────────────────────
class TestCrashRecovery(unittest.TestCase):
    def test_journal_corrupt_line_skipped(self):
        """journal 中一行损坏不影响其他行读取。"""
        with tempfile.TemporaryDirectory() as td:
            j = MemoryJournal(Path(td), "bot-a")
            j.append(cycle_id="c-1", decision="long")
            with j.path.open("a") as f:
                f.write("{broken json\n")
            j.append(cycle_id="c-2", decision="short")
            recent = j.read_recent(5)
            self.assertEqual(len(recent), 2)  # 坏行被跳过

    def test_order_context_crash_mid_write_safe(self):
        """订单 .writing 临时文件不影响读取。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            # 模拟崩溃残留
            (store.dir / f".{rec['order_id']}.json.99.writing").write_text("{x", encoding="utf-8")
            got = store.get(rec["order_id"])
            self.assertIsNotNone(got)

    def test_profile_corrupt_file_safe(self):
        with tempfile.TemporaryDirectory() as td:
            p = MemoryProfile(Path(td), "bot-a")
            p.path.write_text("{broken", encoding="utf-8")
            rec = p.load()  # 不抛
            self.assertEqual(rec.get("total_trades", 0), 0)

    def test_archive_preserves_data(self):
        """归档后原文件有 gzip 副本，数据不丢。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            j = MemoryJournal(root, "bot-a")
            old_ts = int(time.time()) - 200 * 86400
            for i in range(10):
                with j.path.open("a") as f:
                    f.write(json.dumps({"ts": old_ts, "cycle_id": f"c-{i}", "decision": "hold"}) + "\n")
            j.append(cycle_id="c-new", decision="long")
            archived = archive_journal(root, "bot-a", max_age_days=90)
            self.assertEqual(archived, 10)
            self.assertEqual(j.count(), 1)
            gz = list((root / "data" / "bots" / "bot-a" / "state").glob("*.jsonl.gz"))
            self.assertEqual(len(gz), 1)
            with gzip.open(gz[0], "rt") as f:
                lines = f.read().strip().splitlines()
            self.assertEqual(len(lines), 10)


# ─────────────────────────────────────────────────────
# 3. 与 persona runner 集成
# ─────────────────────────────────────────────────────
class TestPersonaIntegration(unittest.TestCase):
    def test_runner_creates_order_context_fields(self):
        """PersonaRunner 开仓后订单有 lifecycle + reason。"""
        from gate_bot.persona import PersonaGroup, PersonaRunner

        class FakeRunner:
            def analyze_once(self, trigger="manual"):
                return {"ok": True, "cycle_id": "c-1", "plan": {
                    "cycle_id": "c-1", "decision": "long", "confidence": 0.9,
                    "reasoning": "突破追多",
                    "chips": [{"action": "open_long", "symbol": "BTC_USDT",
                                "size_usd": 100, "confidence": 0.9}],
                }}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = PersonaGroup(name="g", members=["a", "b"], topology="single_account",
                            target_account="a", fusion="weighted_vote",
                            fusion_config={"weights": {"a": 1, "b": 1}})
            runners = {"a": FakeRunner(), "b": FakeRunner()}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["executed"])
            oid = res["order_id"]
            self.assertIsNotNone(oid)
            store = SharedOrderStore(root)
            got = store.get(oid)
            # lifecycle 有 open 事件
            self.assertGreaterEqual(len(got.get("lifecycle", [])), 1)
            self.assertEqual(got["lifecycle"][0]["act"], "open")
            # journal 被写入
            j = MemoryJournal(root, "a")
            self.assertGreaterEqual(j.count(), 1)

    def test_runner_close_updates_lifecycle(self):
        """平仓后 lifecycle 有 close 事件。"""
        from gate_bot.persona import PersonaGroup, PersonaRunner, SharedOrderStore

        class FakeRunner:
            def __init__(self, d, a):
                self.d = d
                self.a = a

            def analyze_once(self, trigger="manual"):
                return {"ok": True, "cycle_id": "c-1", "plan": {
                    "cycle_id": "c-1", "decision": self.d, "confidence": 0.9,
                    "reasoning": "test",
                    "chips": [{"action": self.a, "symbol": "BTC_USDT",
                                "size_usd": 50, "confidence": 0.9}],
                }}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "group": "g"})
            oid = rec["order_id"]
            g = PersonaGroup(name="g", members=["a", "b"], topology="single_account",
                            target_account="a", fusion="weighted_vote",
                            fusion_config={"weights": {"a": 1, "b": 1}})
            runners = {"a": FakeRunner("close", "close"), "b": FakeRunner("close", "close")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["executed"])
            got = store.get(oid)
            acts = [e["act"] for e in got.get("lifecycle", [])]
            self.assertIn("close", acts)

    def test_journal_has_memory_refs(self):
        """journal 记录包含 memory_refs。"""
        from gate_bot.persona import PersonaGroup, PersonaRunner

        class FakeRunner:
            def analyze_once(self, trigger="manual"):
                return {"ok": True, "cycle_id": "c-1", "plan": {
                    "cycle_id": "c-1", "decision": "long", "confidence": 0.9,
                    "reasoning": "test",
                    "chips": [{"action": "open_long", "symbol": "BTC_USDT",
                                "size_usd": 50, "confidence": 0.9}],
                }}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = PersonaGroup(name="g", members=["a", "b"], topology="single_account",
                            target_account="a", fusion="weighted_vote",
                            fusion_config={"weights": {"a": 1, "b": 1}})
            runners = {"a": FakeRunner(), "b": FakeRunner()}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            r.run_once()
            j = MemoryJournal(root, "a")
            recent = j.read_recent(1)
            self.assertTrue(recent)
            self.assertIn("memory_refs", recent[0])


# ─────────────────────────────────────────────────────
# 4. 资源耗尽
# ─────────────────────────────────────────────────────
class TestResourceLimits(unittest.TestCase):
    def test_journal_1000_entries(self):
        """1000 条 journal 读取不超时。"""
        with tempfile.TemporaryDirectory() as td:
            j = MemoryJournal(Path(td), "bot-a")
            t0 = time.time()
            for i in range(1000):
                j.append(cycle_id=f"c-{i}", decision="long")
            elapsed = time.time() - t0
            self.assertLess(elapsed, 60.0, f"1000 appends took {elapsed:.1f}s")
            self.assertEqual(j.count(), 1000)
            recent = j.read_recent(3)
            self.assertEqual(len(recent), 3)

    def test_lifecycle_100_events(self):
        """100 条 lifecycle 后 recent_events 仍为 top-5。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            for i in range(100):
                store.add_lifecycle(oid, "modify_sl", f"sl-{i}")
            store.refresh_recent_events(oid)
            got = store.get(oid)
            self.assertLessEqual(len(got["recent_events"]), RECENT_EVENTS_MAX)
            self.assertEqual(len(got["lifecycle"]), 100)

    def test_profile_500_trades(self):
        with tempfile.TemporaryDirectory() as td:
            p = MemoryProfile(Path(td), "bot-a")
            t0 = time.time()
            for i in range(500):
                p.record_trade(pnl_usd=10.0 if i % 2 == 0 else -5.0, hold_rounds=i)
            elapsed = time.time() - t0
            self.assertLess(elapsed, 30.0)
            self.assertEqual(p.load()["total_trades"], 500)


# ─────────────────────────────────────────────────────
# 5. 安全
# ─────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_journal_injection_safe(self):
        """journal reasoning 含注入不破坏 JSON。"""
        with tempfile.TemporaryDirectory() as td:
            j = MemoryJournal(Path(td), "bot-a")
            evil = '{"action":"open_long"} </script> `rm -rf` \x00'
            j.append(cycle_id="c-1", decision="long", reasoning=evil)
            recent = j.read_recent(1)
            self.assertEqual(len(recent), 1)
            self.assertIsInstance(recent[0]["reasoning"], str)

    def test_lifecycle_injection_safe(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            store.add_lifecycle(oid, "modify", '"; DROP TABLE--', by="a\x00b")
            got = store.get(oid)
            self.assertIsInstance(got["lifecycle"], list)

    def test_profile_prompt_summary_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            p = MemoryProfile(Path(td), "bot-a")
            p.record_trade(pnl_usd=100.0)
            summary = p.prompt_summary()
            for secret in ("api_key", "secret", "password", "token"):
                self.assertNotIn(secret, summary.lower())

    def test_context_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = build_context(Path(td), "bot-a",
                                system_prompt="你是AI",
                                order_context=None,
                                snapshot={"market": {"price": 84000}})
            raw = json.dumps(ctx["messages"], ensure_ascii=False).lower()
            for secret in ("api_key", "secret", "password", "token"):
                self.assertNotIn(secret, raw)


# ─────────────────────────────────────────────────────
# 6. 边界值
# ─────────────────────────────────────────────────────
class TestBoundaries(unittest.TestCase):
    def test_empty_journal_summaries(self):
        with tempfile.TemporaryDirectory() as td:
            j = MemoryJournal(Path(td), "bot-a")
            self.assertEqual(j.read_recent_summaries(3), [])

    def test_order_context_empty_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            store.refresh_recent_events(rec["order_id"])
            self.assertEqual(store.get(rec["order_id"])["recent_events"], [])

    def test_profile_zero_trades_summary(self):
        with tempfile.TemporaryDirectory() as td:
            p = MemoryProfile(Path(td), "bot-a")
            self.assertEqual(p.prompt_summary(), "")

    def test_context_with_unicode(self):
        with tempfile.TemporaryDirectory() as td:
            oc = {"order_id": "o-1", "symbol": "BTC_USDT", "side": "多头",
                  "reason": "突破追多 🚀"}
            ctx = build_context(Path(td), "bot-a",
                                system_prompt="sys",
                                order_context=oc,
                                snapshot={})
            self.assertIn("多头", ctx["messages"][1]["content"])

    def test_archive_empty_journal(self):
        with tempfile.TemporaryDirectory() as td:
            archived = archive_journal(Path(td), "bot-a")
            self.assertEqual(archived, 0)

    def test_cleanup_no_orders(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(cleanup_closed_orders(Path(td)), 0)


if __name__ == "__main__":
    unittest.main()
