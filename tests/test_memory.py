"""agent-memory 测试：订单上下文扩展 / Journal / Profile / 上下文拼装 / 缓存 / 遗忘。"""
import gzip
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.memory import (  # noqa: E402
    CacheGuard,
    MemoryJournal,
    MemoryProfile,
    archive_journal,
    build_context,
    cleanup_closed_orders,
)
from omnialpha.persona.orders import (  # noqa: E402
    ACT_WEIGHTS,
    RECENT_EVENTS_MAX,
    SharedOrderStore,
    _event_weight,
)


class TestOrderContextExt(unittest.TestCase):
    def test_create_with_memory_fields(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "group": "g"})
            self.assertEqual(rec["lifecycle"], [])
            self.assertEqual(rec["recent_events"], [])
            self.assertEqual(rec["memory_refs"], [])
            self.assertEqual(rec["invalidation"], [])
            self.assertEqual(rec["reason_text"], "")

    def test_set_reason(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            store.set_reason(oid, "突破24h高点追多")
            self.assertIn("突破", store.get(oid)["reason_text"])

    def test_add_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            store.add_lifecycle(oid, "open", "entry=84000", by="fusion:weighted_vote")
            store.add_lifecycle(oid, "modify_sl", "sl→83200", by="brooks")
            got = store.get(oid)
            self.assertEqual(len(got["lifecycle"]), 2)
            self.assertEqual(got["lifecycle"][0]["act"], "open")
            self.assertEqual(got["lifecycle"][1]["by"], "brooks")

    def test_add_memory_ref(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            store.add_memory_ref(oid, "journal:c-001")
            store.add_memory_ref(oid, "journal:c-001")  # 重复不加
            store.add_memory_ref(oid, "journal:c-002")
            self.assertEqual(store.get(oid)["memory_refs"], ["journal:c-001", "journal:c-002"])

    def test_add_invalidation(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            store.add_invalidation(oid, "sl", "83500", "83200")
            got = store.get(oid)
            self.assertEqual(len(got["invalidation"]), 1)
            self.assertEqual(got["invalidation"][0]["field"], "sl")

    def test_refresh_recent_events_top5(self):
        """lifecycle 10 条 → recent_events 只取 top-5。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            for i in range(10):
                act = "open" if i == 0 else ("modify_sl" if i % 2 == 0 else "hold")
                store.add_lifecycle(oid, act, f"event-{i}")
            store.refresh_recent_events(oid)
            got = store.get(oid)
            self.assertLessEqual(len(got["recent_events"]), RECENT_EVENTS_MAX)
            # open 权重最高，应排第一
            self.assertEqual(got["recent_events"][0]["act"], "open")

    def test_get_order_context(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT", "side": "long",
                                "entry_price": 84000, "tp": 85700, "sl": 83500})
            oid = rec["order_id"]
            store.set_reason(oid, "突破追多")
            store.add_lifecycle(oid, "open", "entry=84000")
            ctx = store.get_order_context(oid)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx["symbol"], "BTC_USDT")
            self.assertIn("突破", ctx["reason"])

    def test_get_order_context_closed_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT", "status": "closed"})
            self.assertIsNone(store.get_order_context(rec["order_id"]))

    def test_event_weight_decay(self):
        t_now = 1000000
        w_recent = _event_weight("open", t_now, t_now)
        w_old = _event_weight("open", t_now, t_now - 3600 * 10)  # 10h 前
        self.assertGreater(w_recent, w_old)
        self.assertAlmostEqual(_event_weight("hold", t_now, t_now), ACT_WEIGHTS["hold"])


class TestJournal(unittest.TestCase):
    def test_append_and_read_recent(self):
        with tempfile.TemporaryDirectory() as td:
            j = MemoryJournal(Path(td), "bot-a")
            for i in range(5):
                j.append(cycle_id=f"c-{i}", decision="long", reasoning=f"r{i}")
            recent = j.read_recent(3)
            self.assertEqual(len(recent), 3)
            self.assertEqual(recent[-1]["cycle_id"], "c-4")

    def test_read_recent_summaries(self):
        with tempfile.TemporaryDirectory() as td:
            j = MemoryJournal(Path(td), "bot-a")
            j.append(cycle_id="c-1", decision="short", reasoning="理由" * 20)
            s = j.read_recent_summaries(1)
            self.assertEqual(len(s), 1)
            self.assertEqual(s[0]["decision"], "short")
            # **不再 [:30] 截断** —— 那正是 [近况] 段只有 171 字符、而模型每轮写
            # 6K–76K 字符推理的原因。写入侧已截到 [:200]，这里不再砍第二刀。
            self.assertEqual(s[0]["reasoning"], "理由" * 20)

    def test_count(self):
        with tempfile.TemporaryDirectory() as td:
            j = MemoryJournal(Path(td), "bot-a")
            self.assertEqual(j.count(), 0)
            j.append(cycle_id="c-1", decision="hold")
            self.assertEqual(j.count(), 1)

    def test_append_only_immutable(self):
        """Journal 只有 append，没有 update/delete 方法。"""
        j = MemoryJournal(Path("x"), "bot-a")
        self.assertFalse(hasattr(j, "update"))
        self.assertFalse(hasattr(j, "delete"))

    def test_snapshot_digest(self):
        d = MemoryJournal.snapshot_digest({"a": 1, "b": 2})
        self.assertTrue(d.startswith("sha256:"))
        self.assertEqual(len(d), 19)  # sha256: + 12 hex


class TestProfile(unittest.TestCase):
    def test_record_and_summary(self):
        with tempfile.TemporaryDirectory() as td:
            p = MemoryProfile(Path(td), "bot-a")
            p.record_trade(pnl_usd=50.0, hold_rounds=10)
            p.record_trade(pnl_usd=-20.0, hold_rounds=5)
            rec = p.load()
            self.assertEqual(rec["total_trades"], 2)
            self.assertEqual(rec["win_count"], 1)
            self.assertAlmostEqual(rec["total_pnl_usd"], 30.0)
            summary = p.prompt_summary()
            self.assertIn("2笔", summary)
            self.assertIn("50%", summary)

    def test_empty_profile(self):
        with tempfile.TemporaryDirectory() as td:
            p = MemoryProfile(Path(td), "bot-a")
            self.assertEqual(p.prompt_summary(), "")

    def test_max_drawdown(self):
        with tempfile.TemporaryDirectory() as td:
            p = MemoryProfile(Path(td), "bot-a")
            p.record_trade(pnl_usd=-100.0)
            p.record_trade(pnl_usd=-50.0)
            self.assertAlmostEqual(p.load()["max_drawdown_usd"], -100.0)


class TestContextAssembly(unittest.TestCase):
    def test_build_context_no_position(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = build_context(Path(td), "bot-a",
                                system_prompt="你是交易AI",
                                order_context=None,
                                snapshot={"market": {"price": 84000}})
            self.assertIn("system", ctx["messages"][0]["role"])
            self.assertIn("交易AI", ctx["messages"][0]["content"])
            self.assertIn("无持仓", ctx["messages"][1]["content"])
            self.assertIn("84000", ctx["messages"][1]["content"])

    def test_build_context_with_position(self):
        with tempfile.TemporaryDirectory() as td:
            oc = {
                "order_id": "o-1", "symbol": "BTC_USDT", "side": "long",
                "entry_price": 84000, "tp": 85700, "sl": 83500,
                "reason": "突破追多",
                "recent_events": [{"act": "open", "detail": "entry=84000"}],
                "memory_refs": ["journal:c-1"],
            }
            ctx = build_context(Path(td), "bot-a",
                                system_prompt="sys",
                                order_context=oc,
                                snapshot={"market": {}})
            content = ctx["messages"][1]["content"]
            self.assertIn("BTC_USDT", content)
            self.assertIn("突破追多", content)
            self.assertIn("entry=84000", content)

    def test_context_order_stable(self):
        """system 在前（缓存区），快照在后（变化区）。"""
        with tempfile.TemporaryDirectory() as td:
            ctx = build_context(Path(td), "bot-a",
                                system_prompt="sys",
                                order_context=None,
                                snapshot={"a": 1})
            msgs = ctx["messages"]
            self.assertEqual(msgs[0]["role"], "system")
            self.assertEqual(msgs[1]["role"], "user")


class TestCacheGuard(unittest.TestCase):
    def test_record_and_rate(self):
        with tempfile.TemporaryDirectory() as td:
            g = CacheGuard(Path(td), "bot-a")
            g.record(hit_tokens=1800, total_tokens=3600)
            g.record(hit_tokens=1700, total_tokens=3500)
            rate = g.recent_hit_rate()
            self.assertGreater(rate, 0.4)
            self.assertLess(rate, 0.6)

    def test_prefix_stable(self):
        self.assertTrue(CacheGuard.validate_prefix_stable("abc", "abc"))
        self.assertFalse(CacheGuard.validate_prefix_stable("abc", "abd"))


class TestForgetting(unittest.TestCase):
    def test_archive_journal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            j = MemoryJournal(root, "bot-a")
            # 写 3 条：2 条旧 + 1 条新
            old_ts = int(time.time()) - 100 * 86400
            for i in range(2):
                rec = {"ts": old_ts, "cycle_id": f"c-{i}", "decision": "hold"}
                with j.path.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
            j.append(cycle_id="c-new", decision="long")
            archived = archive_journal(root, "bot-a", max_age_days=90)
            self.assertEqual(archived, 2)
            self.assertEqual(j.count(), 1)
            # 归档文件存在
            gz = list((root / "data" / "bots" / "bot-a" / "state").glob("*.jsonl.gz"))
            self.assertEqual(len(gz), 1)
            with gzip.open(gz[0], "rt") as f:
                lines = f.read().strip().splitlines()
            self.assertEqual(len(lines), 2)

    def test_cleanup_closed_orders(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            # 旧的已平仓单（create 后直接改文件的时间戳）
            rec = store.create({"symbol": "BTC", "status": "closed"})
            old_ts = int(time.time()) - 400 * 86400
            p = store._path(rec["order_id"])
            data = json.loads(p.read_text(encoding="utf-8"))
            data["updated_at"] = old_ts
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            # 新的 open 单
            store.create({"symbol": "ETH", "status": "open"})
            removed = cleanup_closed_orders(root, max_age_days=365)
            self.assertEqual(removed, 1)
            self.assertEqual(len(store.list_open()), 1)


if __name__ == "__main__":
    unittest.main()
