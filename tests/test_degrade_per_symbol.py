# -*- coding: utf-8 -*-
"""降级 / 讨论 / 兜底**不猜币**（B-13/B-14/B-15/B-16、D-21/D-22 / T3 + T8 的策略侧）。

回归背景（2026-10-09 全量审计）：

- `loop.py:519/572` 降级 hold 只覆盖**首币**且硬编码 `BTC_USDT` → 多币宇宙下
  其余币在降级轮里「不存在」，等于把「整轮没分析」伪装成「对 BTC 的判断」。
- `loop.py:839/892` 讨论 chip 的 symbol 兜底链以 `BTC_USDT` 收尾 →
  多币讨论退化成单币。
- `persona/runner.py:655-665` 越界/空 symbol **静默改成 `allowed[0]`** 并照常下单
  （审计里唯一的实盘安全级：价位是 A 币的、下单打的是 B 币）。
- `persona/runner.py:601-623` `_resolve_order_id` 用 `opens[0]` → **可能关错币的单**。

本文件钉住三件事：多币下逐币一条 hold（symbol 集合 == 宇宙）；越界/缺失一律**拒绝**
（不静默改币）；单币路径逐字不变。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from omnialpha.persona.config import PersonaGroup
from omnialpha.persona.runner import PersonaRunner
from omnialpha.strategist.loop import PlanRunner, StrategistConfig
from omnialpha.strategist.risk import RiskConfig


# ─────────────────────────────────────────────────────
# 1. 降级 hold：逐币一条
# ─────────────────────────────────────────────────────
class TestHoldPlanPerSymbol(unittest.TestCase):
    def _runner(self, symbols) -> PlanRunner:
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=list(symbols))
        return r

    def test_single_symbol_unchanged(self):
        plan = self._runner(["BTC_USDT"])._hold_plan("c1", "llm_failed: 502")
        self.assertEqual(len(plan.chips), 1)
        c = plan.chips[0]
        self.assertEqual(c.symbol, "BTC_USDT")
        self.assertEqual(c.action, "hold")
        self.assertEqual(c.confidence, 0.0)
        self.assertEqual(c.reasoning, "数据/模型异常，降级观望")
        self.assertEqual(plan.reasoning, "[降级] llm_failed: 502")

    def test_multi_symbol_one_hold_each(self):
        plan = self._runner(["BTC_USDT", "ETH_USDT", "SOL_USDT"])._hold_plan(
            "c1", "parse_failed: x")
        self.assertEqual([c.symbol for c in plan.chips],
                         ["BTC_USDT", "ETH_USDT", "SOL_USDT"],
                         "降级轮必须逐币一条 hold（symbol 集合 == 宇宙）")
        self.assertEqual({c.action for c in plan.chips}, {"hold"})
        self.assertEqual({c.confidence for c in plan.chips}, {0.0})
        self.assertEqual(plan.reasoning, "[降级] parse_failed: x")

    def test_empty_universe_writes_no_symbol(self):
        plan = self._runner([])._hold_plan("c1", "llm_failed: 502")
        self.assertEqual([c.symbol for c in plan.chips], [],
                         "宇宙为空时不得兜底写 BTC")
        self.assertEqual(plan.raw.get("degraded"), "llm_failed: 502",
                         "没标的可写时必须留痕")


class TestHoldFallbackPerSymbol(unittest.TestCase):
    def _runner(self, symbols) -> PlanRunner:
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=list(symbols))
        return r

    def test_single_symbol_unchanged(self):
        out = self._runner(["BTC_USDT"])._hold_fallback("c1", "manual", "llm_failed: 502")
        self.assertTrue(out["ok"])
        self.assertEqual(out["degraded"], "llm_failed: 502")
        plan = out["plan"]
        self.assertEqual(plan["reasoning"], "[降级] llm_failed: 502")
        self.assertEqual(len(plan["chips"]), 1)
        self.assertEqual(plan["chips"][0], {
            "symbol": "BTC_USDT", "action": "hold", "confidence": 0.0,
            "reasoning": "数据/模型异常，降级观望"})

    def test_multi_symbol_one_hold_each(self):
        out = self._runner(["BTC_USDT", "ETH_USDT"])._hold_fallback(
            "c1", "manual", "llm_failed: 502")
        self.assertEqual([c["symbol"] for c in out["plan"]["chips"]],
                         ["BTC_USDT", "ETH_USDT"])

    def test_empty_universe_writes_no_symbol(self):
        out = self._runner([])._hold_fallback("c1", "manual", "llm_failed: 502")
        self.assertEqual(out["plan"]["chips"], [], "宇宙为空时不得兜底写 BTC")
        self.assertEqual(out["plan"]["meta"]["degraded"], "llm_failed: 502")


# ─────────────────────────────────────────────────────
# 2. 讨论：契约要 symbol，越界就拒绝
# ─────────────────────────────────────────────────────
class _FakeLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def chat(self, system, user):
        self.calls.append((system, user))
        return self.reply


class _DiscussBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def _runner(self, symbols, reply) -> PlanRunner:
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=list(symbols), bot_id="x")
        r.llm = _FakeLLM(reply)
        r.history_dir = Path(self._td.name)
        return r

    @staticmethod
    def _plan(symbol="", action="hold"):
        chips = [{"symbol": symbol, "action": action}] if symbol else []
        return {"decision": "hold", "confidence": 0.5, "reasoning": "原始", "chips": chips}


class TestDiscussionContractHasSymbol(_DiscussBase):
    def test_multi_symbol_contract_requires_symbol(self):
        r = self._runner(["BTC_USDT", "ETH_USDT"],
                         '{"decision":"hold","confidence":0.5,"reasoning":"x"}')
        r.discuss(self._plan(), peers=[], round_num=1, discussion_text="", max_rounds=3)
        sys_prompt = r.llm.calls[0][0]
        self.assertIn('"symbol"', sys_prompt,
                      "多币讨论必须要求模型给出目标币")
        self.assertIn("BTC_USDT", sys_prompt + r.llm.calls[0][1],
                      "契约应让模型看得见可选标的")

    def test_single_symbol_contract_unchanged(self):
        """I11：单币讨论的契约逐字不变（不引入 symbol 字段）。"""
        r = self._runner(["ETH_USDT"],
                         '{"decision":"hold","confidence":0.5,"reasoning":"x"}')
        r.discuss(self._plan(), peers=[], round_num=1, discussion_text="", max_rounds=3)
        self.assertNotIn('"symbol"', r.llm.calls[0][0],
                         "单币宇宙标的唯一，契约不该变（缓存/逐字不变）")


class TestDiscussionSymbolNotGuessed(_DiscussBase):
    def test_out_of_universe_symbol_rejected(self):
        """eth-disc 实测形态：讨论里给出别的币 → 拒绝，不静默改币。"""
        r = self._runner(["ETH_USDT"],
                         '{"decision":"open_long","symbol":"BTC_USDT","confidence":0.9,'
                         '"reasoning":"x","sl":84000,"price":84500}')
        out = r.discuss(self._plan(), peers=[], round_num=1,
                        discussion_text="", max_rounds=3)
        self.assertIsNone(out["chip"], "越界标的的 chip 必须被拒绝")
        self.assertEqual(out["decision"], "hold", "入场却给不出合法标的 → 退回 hold")
        self.assertEqual(out["meta"]["corrected_from"], "BTC_USDT", "拒绝要留痕")
        self.assertEqual(out["meta"]["reason"], "symbol_not_in_universe")

    def test_missing_symbol_rejected_in_multi_universe(self):
        r = self._runner(["BTC_USDT", "ETH_USDT"],
                         '{"decision":"open_long","confidence":0.9,'
                         '"reasoning":"x","sl":84000,"price":84500}')
        out = r.discuss(self._plan(), peers=[], round_num=1,
                        discussion_text="", max_rounds=3)
        self.assertIsNone(out["chip"], "多币宇宙缺 symbol 不得补首币")
        self.assertEqual(out["meta"]["reason"], "symbol_missing")

    def test_valid_symbol_kept(self):
        r = self._runner(["BTC_USDT", "ETH_USDT"],
                         '{"decision":"open_long","symbol":"ETH_USDT","confidence":0.9,'
                         '"reasoning":"x","sl":2694,"price":2700}')
        out = r.discuss(self._plan("ETH_USDT", "hold"), peers=[], round_num=1,
                        discussion_text="", max_rounds=3)
        self.assertIsNotNone(out["chip"])
        self.assertEqual(out["chip"]["symbol"], "ETH_USDT")
        self.assertNotIn("meta", out)

    def test_single_symbol_base_chip_symbol_kept(self):
        r = self._runner(["ETH_USDT"],
                         '{"decision":"open_long","confidence":0.9,'
                         '"reasoning":"x","sl":2694,"price":2700}')
        out = r.discuss(self._plan("ETH_USDT", "hold"), peers=[], round_num=1,
                        discussion_text="", max_rounds=3)
        self.assertEqual(out["chip"]["symbol"], "ETH_USDT",
                         "原 chip 的标的必须保留（eth-disc 回归）")

    def test_default_symbol_only_when_universe_unique(self):
        self.assertEqual(self._runner(["ETH_USDT"], "")._default_symbol(), "ETH_USDT")
        self.assertEqual(self._runner(["BTC_USDT", "ETH_USDT"], "")._default_symbol(), "",
                         "多币宇宙没有「默认币」")
        self.assertEqual(self._runner([], "")._default_symbol(), "")


class TestDiscussionChipNoHardcodedSymbol(unittest.TestCase):
    def test_no_symbol_left_empty_for_caller_to_reject(self):
        chip = PlanRunner._discussion_chip(
            {"decision": "open_long", "sl": 84000, "price": 84500},
            "open_long", {"chips": []})
        self.assertIsNotNone(chip)
        self.assertEqual(chip["symbol"], "",
                         "无法确定标的时留空（由调用方拒绝），不得落到 BTC_USDT")

    def test_model_symbol_wins(self):
        chip = PlanRunner._discussion_chip(
            {"decision": "open_long", "symbol": "ETH_USDT", "sl": 2694, "price": 2700},
            "open_long", {"chips": []})
        self.assertEqual(chip["symbol"], "ETH_USDT")

    def test_default_symbol_still_supported(self):
        """旧调用方（单币组/既有测试）传 default_symbol 时行为不变。"""
        chip = PlanRunner._discussion_chip(
            {"decision": "stop_entry_long", "sl": 2694.0, "trigger_price": 2706.0},
            "stop_entry_long", {"chips": []}, default_symbol="ETH_USDT")
        self.assertEqual(chip["symbol"], "ETH_USDT")


class TestNoHardcodedSymbolInDegradePaths(unittest.TestCase):
    """源码级反断言（防回归）：降级/兜底路径不得再有具体币名字面量。

    **只扫字符串字面量，不扫注释与 docstring**：注释里出现 `BTC_USDT` 是在说明历史
    （"原先硬编码 BTC_USDT"），那正是要保留的证据。连注释一起扫会让「写清为什么」
    与「防回归」互相打架 —— 本测试第一版就是这么误报的。
    """

    @staticmethod
    def _literals(fn) -> list:
        """函数体里**真实出现的字符串字面量**（排除 docstring）。"""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        docs = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
                    and body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
        return [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs]

    def test_loop_sources_clean(self):
        for fn in (PlanRunner._hold_plan, PlanRunner._hold_fallback,
                   PlanRunner._default_symbol, PlanRunner._discussion_chip):
            with self.subTest(fn=fn.__name__):
                self.assertNotIn("BTC_USDT", self._literals(fn))

    def test_runner_sources_clean(self):
        for fn in (PersonaRunner._symbol_of, PersonaRunner._execute,
                   PersonaRunner._resolve_order_id):
            with self.subTest(fn=fn.__name__):
                self.assertNotIn("BTC_USDT", self._literals(fn))


# ─────────────────────────────────────────────────────
# 3. persona 执行：越界/缺失一律拒绝（B-15 / D-22）
# ─────────────────────────────────────────────────────
class _FakeBot:
    def __init__(self, symbols):
        self.symbols = list(symbols)
        self.strategist = {}


def _persona(root: Path, symbols) -> PersonaRunner:
    g = PersonaGroup(name="t", members=["a"], target_account="a",
                     fusion="weighted_vote", on_conflict="hold")
    return PersonaRunner(root, g, {"a": _FakeBot(symbols)}, {})


def _fusion(action="open_long", decision="long") -> dict:
    return {"action": action, "decision": decision, "confidence": 0.7,
            "votes": {"a": decision}, "mode": "weighted_vote"}


def _plans(symbol: str, action: str = "open_long") -> dict:
    return {"a": {"decision": "long", "chips": [
        {"symbol": symbol, "action": action, "size_usd": 100,
         "sl": 84000.0, "tp": 86000.0, "type": "market"}]}}


class TestExecuteRejectsBadSymbol(unittest.TestCase):
    """越界/无法确定的标的 → **拒绝该 chip**：不下单、不落盘信号，只留痕。

    旧行为是静默改成 `allowed[0]`（B-15，审计里唯一的实盘安全级）：模型本意 SOL
    的结论会被执行成 ETH，而"模型当时说的是哪个币"这个事实被丢掉。
    唯一解（单币宇宙）下**漏写** symbol 才自动补 —— 那是无歧义的补全，不是改币。
    """

    def _run(self, root: Path, symbols, symbol, action="open_long"):
        r = _persona(root, symbols)
        res = r._execute(_fusion(action=action), _plans(symbol, action), None)
        files = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
        sig = json.loads(files[0].read_text(encoding="utf-8")) if files else {}
        return res, sig

    def test_out_of_universe_symbol_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            res, sig = self._run(root, ["BTC_USDT", "ETH_USDT"], "SOL_USDT")
            self.assertFalse(res.get("executed"), "越界标的不得执行")
            self.assertTrue(res.get("rejected"))
            self.assertEqual(sig, {}, "被拒的 chip 不该写进 inbox（那是错币下单）")
            meta = res["meta"]
            self.assertEqual(meta["corrected_from"], "SOL_USDT", "拒绝要留痕")
            self.assertEqual(meta["reason"], "symbol_not_in_universe")
            self.assertEqual(meta["universe"], ["BTC_USDT", "ETH_USDT"])
            self.assertEqual(meta["rejected_action"], "open_long",
                             "被丢掉的动作要留在痕里（可归因）")

    def test_single_universe_out_of_universe_symbol_also_rejected(self):
        """单币宇宙也不能静默改币 —— 「唯一解」只对**漏写**成立，不对「写错」成立。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            res, sig = self._run(root, ["ETH_USDT"], "BTC_USDT")
            self.assertFalse(res.get("executed"))
            self.assertEqual(sig, {})
            self.assertEqual(res["meta"]["corrected_from"], "BTC_USDT")
            self.assertEqual(res["meta"]["universe"], ["ETH_USDT"])

    def test_missing_symbol_rejected_in_multi_universe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            res, sig = self._run(root, ["BTC_USDT", "ETH_USDT"], "")
            self.assertFalse(res.get("executed"), "多币下缺 symbol 不得补首币")
            self.assertEqual(sig, {})
            self.assertEqual(res["meta"]["reason"], "symbol_missing")
            self.assertEqual(res["meta"]["corrected_from"], "")

    def test_missing_symbol_autofilled_when_universe_unique(self):
        """单币宇宙 + chip 没写 symbol → 自动补（唯一解；实盘单币行为不变）。"""
        with tempfile.TemporaryDirectory() as td:
            _, sig = self._run(Path(td), ["ETH_USDT"], "")
            self.assertEqual(sig["symbol"], "ETH_USDT")
            self.assertEqual(sig["meta"]["symbol_autofilled"], "ETH_USDT")

    def test_empty_whitelist_does_not_invent_symbol(self):
        """没有白名单可依据时**不写 BTC**（原先的硬编码兜底）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            res, sig = self._run(root, [], "")
            self.assertFalse(res.get("executed"))
            self.assertEqual(sig, {})
            self.assertNotIn("BTC_USDT", json.dumps(res, ensure_ascii=False))

    def test_in_universe_symbol_kept(self):
        with tempfile.TemporaryDirectory() as td:
            _, sig = self._run(Path(td), ["BTC_USDT", "ETH_USDT"], "ETH_USDT")
            self.assertEqual(sig["symbol"], "ETH_USDT")
            self.assertNotIn("symbol_corrected", sig["meta"])
            self.assertNotIn("symbol_autofilled", sig["meta"])


class TestSymbolOfNoHardcodedFallback(unittest.TestCase):
    def test_chip_symbol_wins(self):
        with tempfile.TemporaryDirectory() as td:
            r = _persona(Path(td), ["BTC_USDT", "ETH_USDT"])
            self.assertEqual(r._symbol_of(_plans("ETH_USDT")), "ETH_USDT")

    def test_unique_member_symbol_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            r = _persona(Path(td), ["ETH_USDT"])
            self.assertEqual(r._symbol_of({"a": {"chips": []}}), "ETH_USDT")

    def test_multi_member_symbols_no_guess(self):
        """成员自己管多币且 chip 没带标的 → 不猜（原先是 BTC_USDT 兜底）。"""
        with tempfile.TemporaryDirectory() as td:
            r = _persona(Path(td), ["BTC_USDT", "ETH_USDT"])
            self.assertEqual(r._symbol_of({"a": {"chips": []}}), "")


class TestResolveOrderIdPerSymbol(unittest.TestCase):
    def _with_two_open(self, root: Path) -> PersonaRunner:
        r = _persona(root, ["BTC_USDT", "ETH_USDT"])
        for oid, sym in (("o-btc", "BTC_USDT"), ("o-eth", "ETH_USDT")):
            r.orders.create({"order_id": oid, "symbol": sym, "side": "long",
                             "group": "t", "members": ["a"], "target_account": "a",
                             "status": "open"})
        return r

    def test_reuses_order_of_that_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._with_two_open(Path(td))
            self.assertEqual(r._resolve_order_id("long", {}, symbol="ETH_USDT"), "o-eth",
                             "必须复用该币自己的单，不能拿 opens[0]")
            self.assertEqual(r._resolve_order_id("hold", {}, symbol="BTC_USDT"), "o-btc")

    def test_reversal_closes_only_that_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._with_two_open(Path(td))
            new_id = r._resolve_order_id("short", {}, symbol="ETH_USDT")
            self.assertNotEqual(new_id, "o-eth")
            self.assertEqual(r.orders.get("o-eth")["status"], "closed")
            self.assertEqual(r.orders.get("o-btc")["status"], "open",
                             "反转 ETH 不得关掉 BTC 的单")

    def test_no_symbol_keeps_legacy_behaviour(self):
        """旧调用方（不传 symbol）行为不变。"""
        with tempfile.TemporaryDirectory() as td:
            r = self._with_two_open(Path(td))
            self.assertEqual(r._resolve_order_id("long", {}), "o-btc")


# ─────────────────────────────────────────────────────
# 4. 策略风控块：多币带 symbols / per_symbol（T8 的策略侧）
# ─────────────────────────────────────────────────────
class TestPromptRiskPerSymbol(unittest.TestCase):
    def _runner(self, symbols, **risk_kw) -> PlanRunner:
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=list(symbols), risk=RiskConfig(**risk_kw))
        return r

    def test_single_symbol_block_unchanged(self):
        """I11：单币标的唯一、名额无从谈起 → 风控块**不得多出任何键**。

        多出 `symbols`/`max_chips_per_symbol` 会改 prompt 文本并让缓存前缀失效。
        """
        out = self._runner(["BTC_USDT"])._prompt_risk({"account": {"total": 90.0}})
        for key in ("symbols", "per_symbol", "max_chips_per_symbol"):
            self.assertNotIn(key, out, f"单币风控块不该多出 {key}")

    def test_multi_symbol_block_lists_universe_and_quota(self):
        """多币：名额是按币给的（T8/D8），不告诉模型配额它会以为几条 chip 能挤在同一个币上。"""
        out = self._runner(["BTC_USDT", "ETH_USDT"])._prompt_risk(
            {"account": {"total": 90.0}})
        self.assertEqual(out["symbols"], ["BTC_USDT", "ETH_USDT"])
        self.assertEqual(out["max_chips_per_symbol"], 1)
        self.assertEqual(sorted(out["per_symbol"]), ["BTC_USDT", "ETH_USDT"],
                         "每个币的生效预算都要给全（模型按币出价）")

    def test_per_symbol_override_defaults_to_global(self):
        r = self._runner(["BTC_USDT", "ETH_USDT"])
        out = r._prompt_risk({"account": {"total": 90.0}},
                             per_symbol={"ETH_USDT": {"max_chips": 2}})
        self.assertEqual(out["per_symbol"]["ETH_USDT"]["max_chips"], 2)
        self.assertEqual(out["per_symbol"]["BTC_USDT"]["max_chips"], out["max_chips"],
                         "没被覆盖的币仍用全局值")
        self.assertEqual(out["per_symbol"]["BTC_USDT"]["min_confidence"],
                         out["min_confidence"])

    def test_per_symbol_chip_quota_surfaced(self):
        r = self._runner(["BTC_USDT", "ETH_USDT"])
        setattr(r.cfg.risk, "max_chips_per_symbol", 1)
        out = r._prompt_risk({"account": {"total": 90.0}})
        self.assertEqual(out["max_chips_per_symbol"], 1)

    def test_out_of_universe_override_ignored(self):
        r = self._runner(["BTC_USDT", "ETH_USDT"])
        out = r._prompt_risk({"account": {"total": 90.0}},
                             per_symbol={"SOL_USDT": {"max_chips": 9}})
        self.assertNotIn("SOL_USDT", out.get("per_symbol", {}))


if __name__ == "__main__":
    unittest.main()
