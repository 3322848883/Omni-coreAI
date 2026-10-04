"""讨论结论必须真正进入融合，且不能丢执行字段。

回归背景 1（线上实测 2026-10-03）：`PlanRunner.discuss()` 只返回 `decision`，
而 `fuse_plans._norm_dir` 与 `_execute` 都**优先读 `chips[0].action`** ——
于是讨论改了 `decision`、日志如实记录「改口」，但融合读的仍是讨论前的 chips：

    第 0 轮   d1 hold | d2 stop_entry_long | d3 hold
    第 3 轮   d1 stop_entry_long | d2 stop_entry_long | d3 stop_entry_long  ← 讨论后一致
    融合票   {d1: hold, d2: long, d3: hold}                                ← 用的是第 0 轮

即**讨论是纯日志表演，对最终决策零影响**。修法：讨论结论产出可执行 chip，
`_discuss` 应用修正时连 `chips` 一起改。

回归背景 2（线上实测 2026-10-04，eth-disc 三人格 ETH 讨论组）：修 1 引入的
`merged["chips"] = []` 又**把原始 chip 的 symbol/size_usd/tp/sl 一起丢了** ——
下一轮该人格改回入场类时 `_discussion_chip` 的 base 为空，symbol 落到硬编码
`"BTC_USDT"`。三个成员都只做 ETH_USDT，落盘的信号却是：

    {"symbol": "BTC_USDT", "sl": 2694.0, "trigger_price": 2706.0, ...}   ← 价位是 ETH 的

单币组被 executor 白名单拦住；**多币组会真的错标的成交**。
修法：`_discuss` 保留 chips 只改写 `action`；`_discussion_chip` 的 symbol 兜底
改用 bot 自己的 `symbols[0]`；`_execute` 再加一道白名单校验兜底。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.persona.config import DiscussionConfig, PersonaGroup  # noqa: E402
from omnialpha.persona.fusion import _norm_dir, fuse_plans  # noqa: E402
from omnialpha.persona.runner import PersonaRunner  # noqa: E402
from omnialpha.strategist.loop import PlanRunner  # noqa: E402


class _FakeRunner:
    """discuss() 直接返回预设的修订结论。"""

    def __init__(self, revised):
        self._revised = revised

    def discuss(self, plan, peers, round_num, discussion_text, max_rounds=3):
        return dict(self._revised)


class _FakeBot:
    """只带 _execute 需要的两个属性。"""

    def __init__(self, symbols):
        self.symbols = list(symbols)
        self.strategist = {}


def _plan(action, sl=84000, price=84500, symbol="BTC_USDT"):
    return {
        "ok": True, "decision": action, "reasoning": "原始理由",
        "chips": [{"symbol": symbol, "action": action, "sl": sl, "tp": 85000,
                   "size_usd": 1000, "type": "limit", "price": price}],
    }


def _chip(action, sl=84400, symbol="BTC_USDT"):
    return {"symbol": symbol, "action": action, "sl": sl, "tp": 84800,
            "size_usd": 1200, "type": "limit", "price": 84500}


def _run_discuss(plans, revised_by_bot, rounds=1):
    group = PersonaGroup(
        name="t", members=list(plans.keys()), target_account=list(plans.keys())[0],
        topology="single_account", fusion="weighted_vote", on_conflict="hold",
        discussion=DiscussionConfig(enabled=True, rounds=rounds,
                                    early_exit_on_agreement=False),
    )
    runners = {b: _FakeRunner(r) for b, r in revised_by_bot.items()}
    with tempfile.TemporaryDirectory() as td:
        r = PersonaRunner(Path(td), group, {}, runners)
        merged, _ = r._discuss(dict(plans))
    return merged


class TestDiscussionReachesFusion(unittest.TestCase):
    def _discuss(self, plans, revised_by_bot):
        return _run_discuss(plans, revised_by_bot)

    def test_revision_action_reaches_norm_dir(self):
        """讨论把 hold 改成 open_long 后，融合必须看到 long。"""
        plans = {"a": _plan("hold"), "b": _plan("hold")}
        revised = {
            "a": {"decision": "open_long", "confidence": 0.6, "reasoning": "改多",
                  "chip": _chip("open_long")},
            "b": {"decision": "hold", "confidence": 0.4, "reasoning": "不动", "chip": None},
        }
        merged = self._discuss(plans, revised)
        self.assertEqual(_norm_dir(merged["a"]), "long",
                         "chips[0].action 必须跟着改，否则融合读到的还是旧动作")
        self.assertEqual(_norm_dir(merged["b"]), "hold")

    def test_hold_revision_keeps_chip_fields(self):
        """讨论把 open_long 改成 hold 后：动作必须变 hold，但执行字段不能丢。

        原先这里断言 `merged["a"]["chips"] == []` —— 那正是 symbol/size_usd/tp/sl
        被丢掉的根源（见模块 docstring 的回归背景 2）。
        """
        plans = {"a": _plan("open_long"), "b": _plan("hold")}
        revised = {"a": {"decision": "hold", "confidence": 0.3, "reasoning": "取消",
                         "chip": None}}
        merged = self._discuss(plans, revised)
        self.assertEqual(_norm_dir(merged["a"]), "hold",
                         "chips[0].action 必须改写成 hold，否则融合读到旧动作")
        self.assertEqual(len(merged["a"]["chips"]), 1, "原始 chip 不能被清空")
        self.assertEqual(merged["a"]["chips"][0]["symbol"], "BTC_USDT")
        self.assertEqual(merged["a"]["chips"][0]["size_usd"], 1000)
        self.assertEqual(merged["a"]["chips"][0]["tp"], 85000)

    def test_fusion_votes_reflect_revision(self):
        """融合票必须反映讨论后的立场（2 人改多 → 决策 long）。"""
        plans = {"a": _plan("hold"), "b": _plan("hold"), "c": _plan("hold")}
        revised = {
            "a": {"decision": "open_long", "confidence": 0.7, "reasoning": "改多",
                  "chip": _chip("open_long")},
            "b": {"decision": "open_long", "confidence": 0.7, "reasoning": "改多",
                  "chip": _chip("open_long")},
        }
        merged = self._discuss(plans, revised)
        group = PersonaGroup(name="t", members=["a", "b", "c"], target_account="a",
                             fusion="weighted_vote", on_conflict="hold")
        fusion = fuse_plans(group, merged)
        self.assertEqual(fusion["votes"]["a"], "long")
        self.assertEqual(fusion["votes"]["b"], "long")
        self.assertEqual(fusion["decision"], "long",
                         "讨论后两人改多，融合不能还停在 hold")


class TestDiscussionSymbolNotHardcoded(unittest.TestCase):
    """讨论产出的信号不能带硬编码的 BTC_USDT 标的（eth-disc 实测回归）。"""

    def test_discussion_chip_uses_bot_symbol(self):
        """chip 没有 symbol 时，兜底必须用 bot 自己的标的。"""
        chip = PlanRunner._discussion_chip(
            {"decision": "stop_entry_long", "sl": 2694.0, "trigger_price": 2706.0},
            "stop_entry_long", {"chips": []}, default_symbol="ETH_USDT")
        self.assertIsNotNone(chip)
        self.assertEqual(chip["symbol"], "ETH_USDT",
                         "空 base 重建时不能落到硬编码 BTC_USDT")

    def test_hold_then_entry_sequence_keeps_symbol(self):
        """hold → 入场 的完整序列（eth-disc 实际轨迹）不能让 symbol 掉到硬编码。"""
        plans = {"a": _plan("open_long", symbol="ETH_USDT")}
        revised = {"a": {"decision": "hold", "confidence": 0.3,
                         "reasoning": "薄时段观望", "chip": None}}
        merged = _run_discuss(plans, revised)
        self.assertEqual(_norm_dir(merged["a"]), "hold")
        # 下一轮又改回入场：chip 从 merged 的 chips 重建
        chip = PlanRunner._discussion_chip(
            {"decision": "stop_entry_long", "sl": 2694.0, "trigger_price": 2706.0},
            "stop_entry_long", merged["a"], default_symbol="ETH_USDT")
        self.assertEqual(chip["symbol"], "ETH_USDT",
                         "原始 chip 的 symbol 必须保留")
        self.assertEqual(chip["size_usd"], 1000, "原始 size_usd 必须保留")
        self.assertEqual(chip["tp"], 85000, "原始 tp 必须保留")

    def test_execute_corrects_symbol_outside_whitelist(self):
        """chip 的标的不在该 bot 白名单里 → 纠正成该 bot 自己的标的并留痕。"""
        group = PersonaGroup(name="t", members=["a"], target_account="a",
                             fusion="weighted_vote", on_conflict="hold")
        bots = {"a": _FakeBot(["ETH_USDT"])}
        plans = {"a": {"decision": "long",
                       "chips": [{"symbol": "BTC_USDT", "action": "stop_entry_long",
                                  "sl": 2694.0, "trigger_price": 2706.0,
                                  "type": "market"}]}}
        fusion = {"decision": "long", "action": "stop_entry_long", "confidence": 0.7,
                  "votes": {"a": "long"}, "mode": "weighted_vote"}
        with tempfile.TemporaryDirectory() as td:
            r = PersonaRunner(Path(td), group, bots, {})
            res = r._execute(fusion, plans, None)
            sig = json.loads(Path(res["signal_file"]).read_text(encoding="utf-8"))
        self.assertEqual(sig["symbol"], "ETH_USDT",
                         "白名单外的标的必须被纠正，不能原样写进信号")
        self.assertEqual(sig["meta"]["symbol_corrected"], "BTC_USDT→ETH_USDT")

    def test_execute_keeps_valid_symbol(self):
        """标的本来就在白名单里 → 原样保留，不留纠正痕迹。"""
        group = PersonaGroup(name="t", members=["a"], target_account="a",
                             fusion="weighted_vote", on_conflict="hold")
        bots = {"a": _FakeBot(["ETH_USDT", "BTC_USDT"])}
        plans = {"a": {"decision": "long",
                       "chips": [{"symbol": "BTC_USDT", "action": "stop_entry_long",
                                  "sl": 84000.0, "trigger_price": 84500.0,
                                  "type": "market"}]}}
        fusion = {"decision": "long", "action": "stop_entry_long", "confidence": 0.7,
                  "votes": {"a": "long"}, "mode": "weighted_vote"}
        with tempfile.TemporaryDirectory() as td:
            r = PersonaRunner(Path(td), group, bots, {})
            res = r._execute(fusion, plans, None)
            sig = json.loads(Path(res["signal_file"]).read_text(encoding="utf-8"))
        self.assertEqual(sig["symbol"], "BTC_USDT")
        self.assertNotIn("symbol_corrected", sig["meta"])

    def test_symbol_of_falls_back_to_member_symbols(self):
        """没有 chip 带 symbol 时，共享订单的 symbol 用成员自己的标的。"""
        group = PersonaGroup(name="t", members=["a"], target_account="a",
                             fusion="weighted_vote", on_conflict="hold")
        bots = {"a": _FakeBot(["ETH_USDT"])}
        with tempfile.TemporaryDirectory() as td:
            r = PersonaRunner(Path(td), group, bots, {})
            self.assertEqual(r._symbol_of({"a": {"chips": []}}), "ETH_USDT")


if __name__ == "__main__":
    unittest.main()
