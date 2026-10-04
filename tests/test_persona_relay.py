"""讨论的两种发言模型：sync（同步轮次）与 relay（接力发言）。

区别只在「同一轮里，后发言的人能不能看到前一位刚说的话」：

  sync  —— 同轮三人读**同一份**上一轮快照，轮末统一生效。顺序无关、可复现。
  relay —— 谁说完立刻生效，后面的人看到前面**本轮最新**的立场。信息在轮内传开，
           但引入顺序依赖（`order: fixed` 固定 members 顺序，`random` 每轮洗牌）。

这组测试用「记录每个发言者收到的 peers」的方式把两种模式的差别钉死 ——
断言的是**中间过程**，不是最终结论（两种模式在这几个用例里结论相同）。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.persona.config import (  # noqa: E402
    DiscussionConfig,
    PersonaError,
    PersonaGroup,
)
from omnialpha.persona.runner import PersonaRunner  # noqa: E402


class _RecordingRunner:
    """记录每次 discuss() 收到的 peers，并返回预设修订（None = 不改）。"""

    def __init__(self, revised, seen):
        self._revised = revised
        self._seen = seen

    def discuss(self, plan, peers, round_num, discussion_text, max_rounds=3):
        self._seen.append({
            "round": round_num,
            "peers": [dict(p) for p in peers],
        })
        return dict(self._revised) if self._revised else None


def _plan(action, symbol="ETH_USDT"):
    return {
        "ok": True, "decision": action, "reasoning": "原始理由",
        "chips": [{"symbol": symbol, "action": action, "sl": 2694.0,
                   "tp": 2726.0, "size_usd": 400, "type": "limit", "price": 2702.0}],
    }


def _chip(action, symbol="ETH_USDT"):
    return {"symbol": symbol, "action": action, "sl": 2694.0, "tp": 2726.0,
            "size_usd": 400, "type": "limit", "price": 2702.0}


def _run(members, plans, runners, mode="sync", order="fixed", rounds=1):
    group = PersonaGroup(
        name="t", members=list(members), target_account=members[0],
        topology="single_account", fusion="weighted_vote", on_conflict="hold",
        discussion=DiscussionConfig(enabled=True, rounds=rounds,
                                    early_exit_on_agreement=False,
                                    mode=mode, order=order),
    )
    with tempfile.TemporaryDirectory() as td:
        r = PersonaRunner(Path(td), group, {}, runners)
        merged, used = r._discuss(dict(plans))
        return merged, r.discussion_log, used


def _peer_of(seen, round_num, speaker_index, want_bot):
    """取第 round_num 轮里第 speaker_index 个发言者看到的某个同伴。"""
    calls = [c for c in seen if c["round"] == round_num]
    peers = calls[speaker_index]["peers"]
    return next(p for p in peers if p["bot"] == want_bot)


class TestSyncVsRelay(unittest.TestCase):
    def _runners(self, seen, a_revised=None, b_revised=None):
        return {
            "a": _RecordingRunner(a_revised, seen),
            "b": _RecordingRunner(b_revised, seen),
        }

    def test_sync_later_speaker_sees_previous_round(self):
        """同步：b 看到的 a 仍是**上一轮**的 hold（a 本轮改成 open_long 还没生效）。"""
        seen = []
        runners = self._runners(seen, a_revised={
            "decision": "open_long", "confidence": 0.7,
            "reasoning": "改多", "chip": _chip("open_long")})
        _, _, _ = _run(["a", "b"], {"a": _plan("hold"), "b": _plan("hold")},
                       runners, mode="sync")
        self.assertEqual(_peer_of(seen, 1, 1, "a")["decision"], "hold",
                         "同步模式下 b 不该看到 a 本轮的新立场")

    def test_relay_later_speaker_sees_current_round(self):
        """接力：b 看到的 a 是**本轮刚改**的 open_long。"""
        seen = []
        runners = self._runners(seen, a_revised={
            "decision": "open_long", "confidence": 0.7,
            "reasoning": "改多", "chip": _chip("open_long")})
        _, _, _ = _run(["a", "b"], {"a": _plan("hold"), "b": _plan("hold")},
                       runners, mode="relay")
        self.assertEqual(_peer_of(seen, 1, 1, "a")["decision"], "open_long",
                         "接力模式下 b 必须看到 a 本轮的新立场")

    def test_both_modes_end_with_same_positions(self):
        """两种模式的**最终立场**一致（差别只在过程）。"""
        out = {}
        for mode in ("sync", "relay"):
            seen = []
            runners = self._runners(seen, a_revised={
                "decision": "open_long", "confidence": 0.7,
                "reasoning": "改多", "chip": _chip("open_long")})
            merged, _, _ = _run(["a", "b"], {"a": _plan("hold"), "b": _plan("hold")},
                                runners, mode=mode)
            out[mode] = {b: merged[b]["decision"] for b in ("a", "b")}
        self.assertEqual(out["sync"], out["relay"])
        self.assertEqual(out["sync"], {"a": "open_long", "b": "hold"})

    def test_relay_log_records_round_start_position(self):
        """日志里的 was 必须是**轮开始时**的立场，不是被更新后的值。"""
        seen = []
        runners = self._runners(seen, a_revised={
            "decision": "open_long", "confidence": 0.7,
            "reasoning": "改多", "chip": _chip("open_long")})
        _, log, _ = _run(["a", "b"], {"a": _plan("hold"), "b": _plan("hold")},
                         runners, mode="relay")
        rec = log[1]
        self.assertEqual(rec["positions"]["a"]["was"], "hold")
        self.assertEqual(rec["positions"]["a"]["now"], "open_long")
        self.assertTrue(rec["positions"]["a"]["changed"])
        self.assertEqual(rec["mode"], "relay")


class TestSpeakerOrder(unittest.TestCase):
    def _three(self, seen):
        return {k: _RecordingRunner(None, seen) for k in ("a", "b", "c")}

    def test_fixed_order_follows_members(self):
        seen = []
        _, log, _ = _run(["a", "b", "c"], {k: _plan("hold") for k in "abc"},
                         self._three(seen), mode="relay", order="fixed")
        self.assertEqual(log[1]["order"], ["a", "b", "c"])

    def test_relay_random_shuffles(self):
        """relay + random：本轮顺序被洗牌（用 patch 固定成反转，便于断言）。"""
        seen = []
        with mock.patch("omnialpha.persona.runner.random.shuffle",
                        lambda xs: xs.reverse()):
            _, log, _ = _run(["a", "b", "c"], {k: _plan("hold") for k in "abc"},
                             self._three(seen), mode="relay", order="random")
        self.assertEqual(log[1]["order"], ["c", "b", "a"])

    def test_sync_ignores_random(self):
        """sync 下 order 无意义（三人读同一快照），不应洗牌。"""
        seen = []
        with mock.patch("omnialpha.persona.runner.random.shuffle",
                        lambda xs: xs.reverse()):
            _, log, _ = _run(["a", "b", "c"], {k: _plan("hold") for k in "abc"},
                             self._three(seen), mode="sync", order="random")
        self.assertEqual(log[1]["order"], ["a", "b", "c"])

    def test_random_order_is_a_permutation(self):
        """多次随机后，每轮顺序都仍是 members 的一个排列（不重不漏）。"""
        seen = []
        _, log, _ = _run(["a", "b", "c"], {k: _plan("hold") for k in "abc"},
                         self._three(seen), mode="relay", order="random", rounds=4)
        for rec in log[1:]:
            self.assertEqual(sorted(rec["order"]), ["a", "b", "c"])


class TestDiscussionConfigValidation(unittest.TestCase):
    def test_defaults_are_sync_fixed(self):
        d = DiscussionConfig(enabled=True)
        self.assertEqual(d.mode, "sync")
        self.assertEqual(d.order, "fixed")

    def test_bad_mode_rejected(self):
        with self.assertRaises(PersonaError):
            DiscussionConfig(enabled=True, mode="parallel")

    def test_bad_order_rejected(self):
        with self.assertRaises(PersonaError):
            DiscussionConfig(enabled=True, order="shuffle")

    def test_mode_is_case_insensitive(self):
        self.assertEqual(DiscussionConfig(enabled=True, mode="RELAY").mode, "relay")


if __name__ == "__main__":
    unittest.main()
