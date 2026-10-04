# -*- coding: utf-8 -*-
"""记忆系统的**接线层**测试。

背景：`agent-memory` 的模块（context / cache_guard / forget）早就写好了，
但生产调用点全是 0 —— 是「写了模块，没接线」的死代码，而 spec 的 checkbox
却把它们勾成了 `[x]`。所以这里测的不是模块本身，而是**数据真的流到了目的地**：

- 订单上下文真的进了 prompt（S2.3）
- journal 真的带上了 snapshot_digest / llm_model / prompt_cache_hit_tokens（S2.4）
- 缓存命中真的被记录（S2.6 / T7）
- 遗忘 GC 真的按间隔执行（S2.7）
- invalidation / set_reason / add_memory_ref 真的有生产写入（S2.7 / S2.3）

**每个 PlanRunner 都显式传 `bot_root=临时目录`** —— 否则 `cfg.bot_root or Path.cwd()`
会把记忆文件写进仓库。
"""
from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from omnialpha.memory import CacheGuard, MemoryJournal, MemoryProfile, run_gc
from omnialpha.persona.orders import SharedOrderStore
from omnialpha.persona.runner import PersonaRunner

BOT = "wire-bot"
OID = "o-wire0001"

HOLD_PLAN = json.dumps({
    "cycle_id": "c-wire-1",
    "chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.9}],
})


# ── 测试替身 ────────────────────────────────────────────────

class _RecordingLLM:
    """记录收到的 prompt，并模拟一次 DeepSeek 调用的 usage（含缓存命中）。"""

    def __init__(self, reply: str = HOLD_PLAN, hit: int = 1800, total: int = 3600,
                 model: str = "deepseek-flash"):
        self.reply, self._hit, self._total = reply, hit, total
        self.systems: list[str] = []
        self.users: list[str] = []
        self.usage_total: dict = {}
        self.last_model = model

    def reset_usage(self) -> None:
        self.usage_total = {}

    def chat(self, system: str, user: str) -> str:
        self.systems.append(system)
        self.users.append(user)
        self.usage_total = {
            "prompt_tokens": self._total,
            "prompt_cache_hit_tokens": self._hit,
            "completion_tokens": 100,
        }
        return self.reply

    def cache_hit_tokens(self) -> int:
        return int(self.usage_total.get("prompt_cache_hit_tokens") or 0)

    def prompt_tokens(self) -> int:
        return int(self.usage_total.get("prompt_tokens") or 0)


class _Client:
    """最小交易所客户端（不联网；返回空/占位数据，只为让 collect_snapshot 跑通）。"""

    def public_get(self, path, qs=""):
        return [[1000, "1", "1", "1", "1", "1", "0"]]

    def get_ticker(self, sym):
        return {"last": "84000"}

    def get_last_price(self, sym):
        return 84000.0

    def get_contract(self, sym):
        return SimpleNamespace(quanto_multiplier=0.0001, order_size_round=0,
                               order_price_round=0.1, leverage_max=20)

    def get_contract_stats(self, sym, limit=1):
        return []

    def get_orderbook_top(self, sym, limit=5):
        return {"bids": [], "asks": []}

    def get_account(self):
        return {}

    def get_positions(self):
        return []

    def list_orders(self):
        return []

    def list_price_orders(self, sym):
        return []


def _plan_runner(root: Path, llm):
    from omnialpha.strategist.loop import PlanRunner, StrategistConfig

    cfg = StrategistConfig(
        symbols=["BTC_USDT"], timeframe="15m", candles=5, env="testnet",
        write_hold=True, vision=False, tools={}, prompt_file="",
        bot_root=root, bot_id=BOT,
    )
    return PlanRunner(_Client(), cfg, root / "inbox" / BOT, root / "hist" / BOT, llm=llm)


def _state(root: Path) -> Path:
    return root / "data" / "bots" / BOT / "state"


# ── S2.3 订单上下文进 prompt ─────────────────────────────────

class TestOrderContextReachesPrompt(unittest.TestCase):
    def test_open_order_reason_and_events_in_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            store.create({
                "order_id": OID, "symbol": "BTC_USDT", "side": "long",
                "group": "g", "members": [BOT], "target_account": BOT,
                "status": "open", "tp": 85700, "sl": 83500,
            })
            store.set_reason(OID, "突破24h高点追多")
            store.add_lifecycle(OID, "open", "entry=84000", by="fusion:weighted_vote")
            store.refresh_recent_events(OID)

            llm = _RecordingLLM()
            _plan_runner(root, llm).run_once()

            user = llm.users[-1]
            self.assertIn(OID, user, "订单号没进 prompt")
            self.assertIn("突破24h高点追多", user, "开仓理由没进 prompt（S2.3 的核心）")
            self.assertIn("entry=84000", user, "recent_events 没进 prompt")

    def test_no_order_shows_no_position(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            llm = _RecordingLLM()
            _plan_runner(root, llm).run_once()
            self.assertIn("无持仓", llm.users[-1])

    def test_other_bots_order_is_not_injected(self):
        """别的 bot 的订单不能串进来。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            store.create({
                "order_id": "o-other", "symbol": "ETH_USDT", "side": "long",
                "group": "g2", "members": ["someone-else"],
                "target_account": "someone-else", "status": "open",
            })
            store.set_reason("o-other", "别人的理由")
            llm = _RecordingLLM()
            _plan_runner(root, llm).run_once()
            self.assertNotIn("别人的理由", llm.users[-1])


# ── S2.4 journal 三字段 + S2.6 缓存护栏 ──────────────────────

class TestJournalAndCacheWiring(unittest.TestCase):
    def test_journal_has_digest_model_and_cache_hit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            llm = _RecordingLLM(hit=1800, total=3600)
            _plan_runner(root, llm).run_once()

            lines = (root / "data" / "bots" / BOT / "state" /
                     "memory_journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertTrue(lines, "journal 没写")
            rec = json.loads(lines[-1])
            self.assertTrue(rec["snapshot_digest"].startswith("sha256:"),
                            f"snapshot_digest 仍为空：{rec['snapshot_digest']!r}")
            self.assertEqual(rec["llm_model"], "deepseek-flash")
            self.assertEqual(rec["prompt_cache_hit_tokens"], 1800,
                             "缓存命中没落进 journal（设计里这是成本主杠杆）")

    def test_cache_stats_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _plan_runner(root, _RecordingLLM(hit=1800, total=3600)).run_once()
            p = root / "data" / "bots" / BOT / "state" / "cache_stats.jsonl"
            self.assertTrue(p.is_file(), "CacheGuard 没被调用")
            rec = json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(rec["hit"], 1800)
            self.assertEqual(rec["total"], 3600)
            self.assertAlmostEqual(rec["hit_rate"], 0.5, places=3)
            self.assertEqual(rec["model"], "deepseek-flash")

    def test_prefix_hash_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _plan_runner(root, _RecordingLLM()).run_once()
            self.assertTrue((root / "data" / "bots" / BOT / "state" /
                             "cache_prefix.sha256").is_file(),
                            "前缀稳定校验没有落盘")

    def test_cache_guard_detects_prefix_change(self):
        with tempfile.TemporaryDirectory() as td:
            g = CacheGuard(Path(td), BOT)
            self.assertTrue(g.check_prefix("同一段前缀"), "首次运行应视为稳定")
            self.assertTrue(g.check_prefix("同一段前缀"))
            self.assertFalse(g.check_prefix("换了前缀"), "前缀变了必须报不稳定")


# ── S2.7 遗忘 GC ────────────────────────────────────────────

class TestForgetWiring(unittest.TestCase):
    def test_run_gc_respects_interval(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            MemoryJournal(root, BOT).append(cycle_id="c-1", decision="hold")
            first = run_gc(root, bot_id=BOT)
            self.assertTrue(first["ran"], "首次应执行")
            second = run_gc(root, bot_id=BOT)
            self.assertFalse(second["ran"], "同一间隔内不该重复执行")

    def test_run_gc_archives_old_journal(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            j = MemoryJournal(root, BOT)
            old = int(time.time()) - 200 * 86400
            with j.path.open("a", encoding="utf-8") as f:
                for i in range(3):
                    f.write(json.dumps({"ts": old, "cycle_id": f"c-{i}"}) + "\n")
            j.append(cycle_id="c-new", decision="long")
            res = run_gc(root, bot_id=BOT)
            self.assertEqual(res["archived"], 3)
            self.assertEqual(j.count(), 1)


# ── persona 侧：invalidation / reason / memory_ref ──────────

def _persona_runner(root: Path) -> PersonaRunner:
    r = PersonaRunner.__new__(PersonaRunner)
    r.root = root
    r.group = SimpleNamespace(members=[BOT], name="g", topology="single_account",
                              target_account=BOT, fusion_config={})
    r.orders = SharedOrderStore(root)
    r.plan_runners = {}
    r.bots = {}
    r.discussion_log = []
    r.log_path = root / "data" / "shared" / "persona_log.jsonl"
    r.log_path.parent.mkdir(parents=True, exist_ok=True)
    return r


class TestPersonaMemoryWiring(unittest.TestCase):
    def test_invalidation_recorded_on_sl_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _persona_runner(root)
            r.orders.create({
                "order_id": OID, "symbol": "BTC_USDT", "side": "long",
                "members": [BOT], "target_account": BOT, "status": "open",
                "tp": 85700.0, "sl": 83500.0,
            })
            r._note_invalidation(OID, {"tp": 86000.0, "sl": 83500.0})
            rec = r.orders.get(OID)
            self.assertEqual(len(rec["invalidation"]), 1, f"应记 1 条失效，实际 {rec['invalidation']}")
            self.assertEqual(rec["invalidation"][0]["field"], "tp")
            self.assertEqual(rec["invalidation"][0]["old"], "85700.0")
            self.assertEqual(rec["invalidation"][0]["new"], "86000.0")

    def test_no_invalidation_when_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _persona_runner(root)
            r.orders.create({
                "order_id": OID, "symbol": "BTC_USDT", "side": "long",
                "members": [BOT], "target_account": BOT, "status": "open",
                "tp": 85700.0, "sl": 83500.0,
            })
            r._note_invalidation(OID, {"tp": 85700.0, "sl": 83500.0})
            self.assertEqual(r.orders.get(OID)["invalidation"], [])

    def test_open_records_reason_and_memory_ref(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _persona_runner(root)
            r.orders.create({
                "order_id": OID, "symbol": "BTC_USDT", "side": "long",
                "members": [BOT], "target_account": BOT, "status": "open",
            })
            fusion = {"action": "open_long", "decision": "long",
                      "mode": "weighted_vote", "reason": "融合理由",
                      "votes": {BOT: "long"}}
            plans = {BOT: {"reasoning": "突破24h高点追多"}}
            r._post_exec_hooks(OID, fusion, plans, {"executed": True}, "c-42")

            rec = r.orders.get(OID)
            self.assertEqual(rec["reason_text"], "突破24h高点追多",
                             "开仓理由没落进订单上下文（S2.3）")
            self.assertIn("journal:c-42", rec["memory_refs"],
                          "决策没引用 journal 索引（FinPos memory_refs）")
            self.assertTrue(rec["recent_events"], "recent_events 没刷新")

    def test_persona_journal_has_new_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _persona_runner(root)
            llm = _RecordingLLM(hit=900, total=2000, model="ds-x")
            llm.usage_total = {"prompt_tokens": 2000, "prompt_cache_hit_tokens": 900}
            r.plan_runners = {BOT: SimpleNamespace(llm=llm, last_snapshot_digest="sha256:abc123")}
            r.orders.create({
                "order_id": OID, "symbol": "BTC_USDT", "side": "long",
                "members": [BOT], "target_account": BOT, "status": "open",
            })
            r._post_exec_hooks(OID, {"action": "open_long", "decision": "long",
                                     "mode": "weighted_vote", "votes": {BOT: "long"}},
                               {BOT: {"reasoning": "r"}}, {"executed": True}, "c-9")
            lines = (root / "data" / "bots" / BOT / "state" /
                     "memory_journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
            rec = json.loads(lines[-1])
            self.assertEqual(rec["snapshot_digest"], "sha256:abc123")
            self.assertEqual(rec["llm_model"], "ds-x")
            self.assertEqual(rec["prompt_cache_hit_tokens"], 900)


# ── 回归钉：不许再变成「有模块、无调用点」 ───────────────────

def _prod_call_files(target: str) -> set[str]:
    """扫 `omnialpha/`（排除 memory 包自身）里真实出现的调用表达式。"""
    root = Path(__file__).resolve().parents[1] / "omnialpha"
    out: set[str] = set()
    for p in root.rglob("*.py"):
        if "memory" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
        except Exception:  # noqa: BLE001
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else "")
            if name == target:
                out.add(str(p.relative_to(root.parent)))
    return out


class TestNoDeadMemoryModules(unittest.TestCase):
    """这四样曾经是死代码（生产调用 0），钉住别再退化。"""

    def test_build_context_called_in_production(self):
        self.assertTrue(_prod_call_files("build_context"),
                        "build_context 又没有生产调用点了（S2.6 上下文组装）")

    def test_cache_guard_called_in_production(self):
        self.assertTrue(_prod_call_files("CacheGuard"),
                        "CacheGuard 又没有生产调用点了（S2.6 / T7）")

    def test_run_gc_called_in_production(self):
        self.assertTrue(_prod_call_files("run_gc"),
                        "遗忘 GC 又没有生产调用点了（S2.7）")

    def test_order_context_called_in_production(self):
        self.assertTrue(_prod_call_files("get_order_context"),
                        "订单上下文又没有生产调用点了（S2.3）")

    def test_invalidation_written_in_production(self):
        self.assertTrue(_prod_call_files("add_invalidation"),
                        "invalidation 又只被读、从没被写了（S2.7）")

    def test_memory_ref_written_in_production(self):
        self.assertTrue(_prod_call_files("add_memory_ref"),
                        "memory_refs 又没有生产写入点了（S2.3）")


# ── persona 分析路径（analyze_once）也要有记忆 ──────────────

class TestPersonaAnalysisPathMemory(unittest.TestCase):
    """`analyze_once` 是 persona-run 真正走的路径，且人格组**正是持有订单**的那条。

    原先它自己手写一套 prompt 拼装、完全不注入记忆 —— 只接 `run_once` 就等于
    「有记忆的单 bot、没记忆的人格」。
    """

    def test_analyze_once_injects_order_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            store.create({
                "order_id": OID, "symbol": "BTC_USDT", "side": "long",
                "group": "g", "members": [BOT], "target_account": BOT,
                "status": "open",
            })
            store.set_reason(OID, "人格路径的开仓理由")
            store.add_lifecycle(OID, "open", "entry=84000")
            store.refresh_recent_events(OID)

            llm = _RecordingLLM()
            _plan_runner(root, llm).analyze_once()
            self.assertIn("人格路径的开仓理由", llm.users[-1],
                          "analyze_once（persona-run 走的路径）没注入订单上下文")

    def test_analyze_once_records_cache_stats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _plan_runner(root, _RecordingLLM(hit=700, total=1400)).analyze_once()
            p = root / "data" / "bots" / BOT / "state" / "cache_stats.jsonl"
            self.assertTrue(p.is_file(), "analyze_once 没记录缓存命中（S2.6/T7）")
            rec = json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(rec["hit"], 700)

    def test_analyze_once_sets_snapshot_digest(self):
        """persona 侧写 journal 时从这里取 `snapshot_digest`。

        只在 `run_once` 里设过 → 实测 persona 写出的 journal 这一格永远是空的。
        """
        with tempfile.TemporaryDirectory() as td:
            runner = _plan_runner(Path(td), _RecordingLLM())
            self.assertEqual(runner.last_snapshot_digest, "")
            runner.analyze_once()
            self.assertTrue(runner.last_snapshot_digest.startswith("sha256:"),
                            f"analyze_once 没设 snapshot_digest：{runner.last_snapshot_digest!r}")


class TestPersonaSignalCarriesExecutionFields(unittest.TestCase):
    """人格的 chip 必须把执行必需字段带到 executor —— 尤其 `trigger_price`。

    原先 `analyze_once` 手抄 7 个字段、`_execute` 又手抄 5 个，
    `trigger_price` 在**源头**就丢了 → stop_entry_* 被 executor 整笔拒掉。
    """

    def test_chips_out_preserves_trigger_price(self):
        reply = json.dumps({
            "cycle_id": "c-trig",
            "chips": [{"symbol": "BTC_USDT", "action": "stop_entry_short",
                       "confidence": 0.8, "size_usd": 100,
                       "trigger_price": 84400, "tp": 83890, "sl": 84720}],
        })
        with tempfile.TemporaryDirectory() as td:
            out = _plan_runner(Path(td), _RecordingLLM(reply=reply)).analyze_once()
            chip = out["plan"]["chips"][0]
            self.assertEqual(chip.get("action"), "stop_entry_short")
            self.assertEqual(chip.get("trigger_price"), 84400,
                             f"chip 在源头丢了 trigger_price：{chip}")

    def test_execute_payload_carries_trigger_price(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _persona_runner(root)
            r.orders.create({
                "order_id": OID, "symbol": "BTC_USDT", "side": "short",
                "members": [BOT], "target_account": BOT, "status": "open",
            })
            captured: dict = {}
            r._exec_single = lambda payload, oid: (captured.update(payload)
                                                   or {"executed": True})
            fusion = {"action": "stop_entry_short", "decision": "short",
                      "mode": "weighted_vote", "reason": "x", "votes": {BOT: "short"}}
            plans = {BOT: {"chips": [{"symbol": "BTC_USDT", "action": "stop_entry_short",
                                      "size_usd": 100, "trigger_price": 84400,
                                      "tp": 83890, "sl": 84720}]}}
            r._execute(fusion, plans, OID)
            self.assertEqual(captured.get("trigger_price"), 84400,
                             f"executor 收到的 payload 缺 trigger_price：{captured}")


class TestHoldCyclesAreJournaled(unittest.TestCase):
    """设计 S2.4 要求**每轮**都进 journal（含 hold）。

    原先 persona 的 hold 分支在 `_post_exec_hooks` 之前就 `return`，
    且 `_post_exec_hooks` 开头还有 `if not order_id: return` —— 于是 hold 轮零留痕。
    实测证据：真实跑一轮（决策 hold、无成交）后三个 bot 的 journal 零新增。
    而近况摘要与缓存命中曲线都靠 journal，只在成交时记会让「近况」几乎空白。
    """

    def test_hold_with_no_order_still_journals(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _persona_runner(root)
            r._post_exec_hooks(
                None,
                {"action": "hold", "decision": "hold", "mode": "weighted_vote",
                 "votes": {BOT: "hold"}},
                {BOT: {"reasoning": "观望等伦敦KZ"}},
                {"executed": False}, "c-hold-1",
            )
            p = root / "data" / "bots" / BOT / "state" / "memory_journal.jsonl"
            self.assertTrue(p.is_file(), "hold 轮没有写 journal")
            rec = json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(rec["cycle_id"], "c-hold-1")
            self.assertEqual(rec["decision"], "hold")
            self.assertFalse(rec["executed"])
            self.assertEqual(rec["memory_refs"], [], "没有单时不该编造 order 引用")

    def test_hold_early_return_calls_hooks_before_returning(self):
        """回归钉：hold 分支必须在 `return` 之前调 `_post_exec_hooks`。"""
        src = inspect.getsource(PersonaRunner.run_once)
        start = src.index('if decision == DIR_HOLD and action in ("hold", "")')
        end = src.index("return result", start)
        branch = src[start:end]
        self.assertIn("_post_exec_hooks(", branch,
                      "hold 分支又绕过 journal 了（每轮都该记，设计 S2.4）")


if __name__ == "__main__":
    unittest.main()
