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
from omnialpha.strategist.tools import NATIVE_TOOLS, TOOL_NAMES, run_tool

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

    def test_group_member_does_not_see_position_in_another_account(self):
        """独立跑 plan 的 bot 不该看到「自己账户上没有」的仓位。

        实测：`smc-paper` 是 `disc-trio` 的分析成员，而该组的单落在 `pa-a` 账户上；
        按 `members` 匹配会把 pa-a 的仓位注入 smc-paper 的 prompt（它自己账户是平的）。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            store.create({
                "order_id": "o-group", "symbol": "BTC_USDT", "side": "short",
                "group": "g", "members": [BOT, "analyst-2"],
                "target_account": "someone-else", "status": "open",
            })
            store.set_reason("o-group", "落在别人账户的仓位理由")

            llm = _RecordingLLM()
            _plan_runner(root, llm).run_once()          # 单 bot 路径
            self.assertNotIn("落在别人账户的仓位理由", llm.users[-1],
                             "单 bot 路径注入了别人账户的仓位")

            llm2 = _RecordingLLM()
            _plan_runner(root, llm2).analyze_once()     # 人格路径
            self.assertIn("落在别人账户的仓位理由", llm2.users[-1],
                          "人格路径应当看到本组共管的仓位（设计 S2.8）")


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


class TestMemoryRefsReachJournal(unittest.TestCase):
    """模型输出的 `memory_refs` 必须真的落进 journal。

    背景：模型每轮都在输出这个字段（实测 cycle 213 的原文
    `"memory_refs": ["...212","...211","...210"]`），但 `schema.py` 的 `Plan`
    没有这个字段、`parse_plan` 也不取 —— 它进了 `plan.raw` 就再没人看。
    实测结果：`memory_journal.jsonl` 896 条记录，`memory_refs` 非空 **0 条**
    （覆盖 148.6 小时）。同时 prompt 还在要求模型输出它 —— 「要求了但不消费」。
    """

    REPLY = json.dumps({
        "cycle_id": "c-mem-1",
        "reasoning": "r",
        "memory_refs": ["c-old-1", "c-old-2"],
        "chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.9}],
    })

    def _last_journal(self, root: Path) -> dict:
        p = root / "data" / "bots" / BOT / "state" / "memory_journal.jsonl"
        return json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])

    def test_refs_land_in_journal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _plan_runner(root, _RecordingLLM(reply=self.REPLY)).run_once()
            self.assertEqual(self._last_journal(root)["memory_refs"], ["c-old-1", "c-old-2"],
                             "模型输出的 memory_refs 没落进 journal")

    def test_bad_type_degrades_to_empty(self):
        """格式瑕疵不该毁掉整轮决策 —— 这是观测性字段，不是控制流。"""
        reply = json.dumps({
            "cycle_id": "c-mem-2", "reasoning": "r",
            "memory_refs": "not-a-list",
            "chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.9}],
        })
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _plan_runner(root, _RecordingLLM(reply=reply)).run_once()
            self.assertEqual(self._last_journal(root)["memory_refs"], [])

    def test_missing_field_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _plan_runner(root, _RecordingLLM()).run_once()      # HOLD_PLAN 无该字段
            self.assertEqual(self._last_journal(root)["memory_refs"], [])

    def test_blank_entries_dropped(self):
        reply = json.dumps({
            "cycle_id": "c-mem-3", "reasoning": "r",
            "memory_refs": ["c-a", "", None, "   ", "c-b"],
            "chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.9}],
        })
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _plan_runner(root, _RecordingLLM(reply=reply)).run_once()
            self.assertEqual(self._last_journal(root)["memory_refs"], ["c-a", "c-b"])


class TestRecentWindowIsUsable(unittest.TestCase):
    """近况窗口必须让模型看得到「最近做过什么」。

    背景：`[近况]` 原先只回看 3 轮、每轮 `reasoning[:30]` —— 实测该段共 **171 字符**，
    而模型每轮产出 6,286–76,211 字符的推理。148 小时的决策历史里，模型看不到自己
    一小时之前做过什么。`journal.py` 存的是 `[:500]`，是 `context.py` 又砍了一刀。

    修法是**索引而不是正文**：正文给最近几轮，更早的只留 `cycle_id + decision` 一行，
    细节用 `journal_lookup` 按需取 —— 这样体积不随历史增长。
    """

    def _seed(self, root: Path, n: int, start: int = 0) -> None:
        j = MemoryJournal(root, BOT)
        for i in range(start, start + n):
            j.append(cycle_id=f"c-{i:03d}", decision="hold",
                     reasoning=f"第{i}轮推理" + "细节" * 40)

    def _user(self, root: Path, **kw) -> str:
        from omnialpha.memory import build_context
        return build_context(root, BOT, system_prompt="SYS",
                             snapshot_text="【市场与账户快照】\n{}", **kw)["user"]

    def test_summary_not_truncated_to_30(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed(root, 1)
            # 断言 40 个字符的连续片段：`[:30]` 截断后只剩 30 字，必然找不到。
            # （断言 "细节"*10 是假阳性 —— 截断后仍留 12 个。）
            self.assertIn("细节" * 20, self._user(root),
                          "近况摘要仍被硬截断到 30 字")

    def test_index_lists_earlier_rounds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed(root, 25)                      # c-000 .. c-024
            user = self._user(root)
            # 索引层覆盖最近 20 轮（c-005..c-024）—— 比摘要层的 3 轮宽得多
            self.assertIn("c-005", user, "索引层没覆盖最近 20 轮")
            self.assertNotIn("c-000", user, "索引层超出了 n_index 窗口")
            self.assertIn("journal_lookup", user, "索引层没告诉模型怎么取细节")

    def test_block_size_does_not_grow_with_history(self):
        """体积不随历史增长 —— 这是它替代「回看 N 轮」的关键。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed(root, 10)
            small = self._user(root)
            self._seed(root, 500, start=10)
            big = self._user(root)
            self.assertLess(abs(len(big) - len(small)), 200,
                            f"近况段随历史增长：10 轮 {len(small)} → 510 轮 {len(big)}")
            self.assertLess(len(big), 2000, f"近况段过大：{len(big)} 字符")

    def test_last_plan_state_survives(self):
        """索引层不能把 [上轮方案状态] 挤掉（Tier 1 字段靠它跨轮）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            MemoryJournal(root, BOT).append(
                cycle_id="c-x", decision="hold", reasoning="r",
                region="trend", invalidation_price=85550.0, time_stop_bars=10)
            self.assertIn("前提失效=85550", self._user(root))


class TestJournalLookupTool(unittest.TestCase):
    """`journal_lookup` 让「记忆」可检索，而不是把 N 轮正文预载进 prompt。

    索引层（`[近期决策索引]`）只给 `cycle_id + decision` 一行，模型要某轮细节时用这个
    工具取回 —— 这是 `[近况]` 体积**不随历史增长**的前提。没有它，索引就只是一串
    无法兑现的编号。
    """

    def test_returns_record_by_cycle_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            j = MemoryJournal(root, BOT)
            j.append(cycle_id="c-a", decision="hold", reasoning="甲")
            j.append(cycle_id="c-b", decision="open_long", reasoning="乙",
                     exec_result={"orders": 1})
            out = run_tool(_Client(), "journal_lookup", {"cycle_id": "c-a"},
                           bot_root=root, bot_id=BOT)
            self.assertEqual(out["decision"], "hold")
            self.assertEqual(out["reasoning"], "甲")

    def test_returns_exec_result_for_detail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            MemoryJournal(root, BOT).append(
                cycle_id="c-b", decision="open_long", exec_result={"orders": 1})
            out = run_tool(_Client(), "journal_lookup", {"cycle_id": "c-b"},
                           bot_root=root, bot_id=BOT)
            self.assertEqual(out["exec_result"], {"orders": 1})

    def test_unknown_cycle_id_returns_error_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            MemoryJournal(root, BOT).append(cycle_id="c-a", decision="hold")
            out = run_tool(_Client(), "journal_lookup", {"cycle_id": "c-nope"},
                           bot_root=root, bot_id=BOT)
            self.assertIn("error", out, "查不到时应返回结构化错误，而不是抛异常")
            self.assertNotIn("decision", out)

    def test_missing_cycle_id_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            out = run_tool(_Client(), "journal_lookup", {},
                           bot_root=Path(td), bot_id=BOT)
            self.assertIn("error", out)

    def test_tool_is_registered_in_schema_and_names(self):
        """模型看不到就调不到 —— schema 与 TOOL_NAMES 两处都要有。"""
        self.assertIn("journal_lookup", TOOL_NAMES)
        names = [(t.get("function") or {}).get("name") for t in NATIVE_TOOLS]
        self.assertIn("journal_lookup", names)


class TestPromptTailIsNotDuplicated(unittest.TestCase):
    """收尾指令只能出现一次。

    背景：`prompt.py:209` 与 `context.py:71` 各写了一句，而 `build_context` 把
    `build_user_prompt` 的**整个输出**当作 `snapshot_text` 塞进模板 —— 于是 user
    prompt 末尾连着两句几乎一样的收尾指令（实测 2026-10-06 的 user prompt 原文）。
    纯冗余，也让「到底该听哪句」变得含糊。
    """

    def test_single_tail_in_run_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            llm = _RecordingLLM()
            _plan_runner(root, llm).run_once()
            user = llm.users[-1]
            self.assertEqual(
                user.count("请输出"), 1,
                f"收尾指令出现 {user.count('请输出')} 次（应只 1 次）：…{user[-200:]}")

    def test_tail_still_asks_for_memory_refs(self):
        """去重不能把 memory_refs 的要求一起删掉。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            llm = _RecordingLLM()
            _plan_runner(root, llm).run_once()
            self.assertIn("memory_refs", llm.users[-1],
                          "去重时把 memory_refs 要求一起丢了")

    def test_fallback_tail_when_snapshot_text_empty(self):
        """`build_context` 的模板要能独立成立：snapshot_text 为空时补收尾句。"""
        from omnialpha.memory import build_context

        with tempfile.TemporaryDirectory() as td:
            ctx = build_context(Path(td), BOT, system_prompt="SYS", snapshot_text="")
            self.assertIn("Plan JSON", ctx["user"],
                          "snapshot_text 为空时模板没有收尾指令，兜底路径会让模型不知该输出什么")


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

    def test_direction_reversal_starts_a_new_order(self):
        """方向反转必须**另起一张单** —— 否则注入的订单上下文会把持仓方向说反。

        实测：账户已是 +177 多仓，订单记录却仍写着 `side=short`、
        理由是「卖墙吸收…限价空」—— 把持仓方向说反比没有记忆更危险。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _persona_runner(root)
            oid_long = r._resolve_order_id("long", {})
            r.orders.create({"order_id": oid_long, "symbol": "BTC_USDT", "side": "long",
                             "group": "g", "members": [BOT], "target_account": BOT,
                             "status": "open"})
            # 同向 → 复用（共同记忆贯穿持仓期）
            self.assertEqual(r._resolve_order_id("long", {}), oid_long)

            # 反向 → 新单 + 旧单关闭
            oid_short = r._resolve_order_id("short", {})
            self.assertNotEqual(oid_short, oid_long, "方向反转应另起一张单")
            self.assertEqual(r.orders.get(oid_long)["status"], "closed",
                             "反转后旧单必须关闭，否则它会继续进 prompt")

            r.orders.create({"order_id": oid_short, "symbol": "BTC_USDT", "side": "short",
                             "group": "g", "members": [BOT], "target_account": BOT,
                             "status": "open"})
            # hold / 管理动作仍复用当前方向的单
            self.assertEqual(r._resolve_order_id("hold", {}), oid_short)
            self.assertEqual(r._resolve_order_id("reduce", {}), oid_short)

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
