"""讨论结论必须真正进入融合。

回归背景（线上实测 2026-10-03）：`PlanRunner.discuss()` 只返回 `decision`，
而 `fuse_plans._norm_dir` 与 `_execute` 都**优先读 `chips[0].action`** ——
于是讨论改了 `decision`、日志如实记录「改口」，但融合读的仍是讨论前的 chips：

    第 0 轮   d1 hold | d2 stop_entry_long | d3 hold
    第 3 轮   d1 stop_entry_long | d2 stop_entry_long | d3 stop_entry_long  ← 讨论后一致
    融合票   {d1: hold, d2: long, d3: hold}                                ← 用的是第 0 轮

即**讨论是纯日志表演，对最终决策零影响**。修法：讨论结论产出可执行 chip，
`_discuss` 应用修正时连 `chips` 一起改。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.persona.config import DiscussionConfig, PersonaGroup  # noqa: E402
from omnialpha.persona.fusion import _norm_dir, fuse_plans  # noqa: E402
from omnialpha.persona.runner import PersonaRunner  # noqa: E402


class _FakeRunner:
    """discuss() 直接返回预设的修订结论。"""

    def __init__(self, revised):
        self._revised = revised

    def discuss(self, plan, peers, round_num, discussion_text, max_rounds=3):
        return dict(self._revised)


def _plan(action, sl=84000, price=84500):
    return {
        "ok": True, "decision": action, "reasoning": "原始理由",
        "chips": [{"symbol": "BTC_USDT", "action": action, "sl": sl, "tp": 85000,
                   "size_usd": 1000, "type": "limit", "price": price}],
    }


def _chip(action, sl=84400):
    return {"symbol": "BTC_USDT", "action": action, "sl": sl, "tp": 84800,
            "size_usd": 1200, "type": "limit", "price": 84500}


class TestDiscussionReachesFusion(unittest.TestCase):
    def _discuss(self, plans, revised_by_bot):
        group = PersonaGroup(
            name="t", members=list(plans.keys()), target_account=list(plans.keys())[0],
            topology="single_account", fusion="weighted_vote", on_conflict="hold",
            discussion=DiscussionConfig(enabled=True, rounds=1,
                                        early_exit_on_agreement=False),
        )
        runners = {b: _FakeRunner(r) for b, r in revised_by_bot.items()}
        with tempfile.TemporaryDirectory() as td:
            r = PersonaRunner(Path(td), group, {}, runners)
            merged, _ = r._discuss(dict(plans))
        return merged

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

    def test_hold_revision_clears_stale_chip(self):
        """讨论把 open_long 改成 hold 后，旧 chip 不能残留。"""
        plans = {"a": _plan("open_long"), "b": _plan("hold")}
        revised = {"a": {"decision": "hold", "confidence": 0.3, "reasoning": "取消",
                         "chip": None}}
        merged = self._discuss(plans, revised)
        self.assertEqual(_norm_dir(merged["a"]), "hold")
        self.assertEqual(merged["a"]["chips"], [])

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


if __name__ == "__main__":
    unittest.main()
