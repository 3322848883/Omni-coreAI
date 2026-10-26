"""整轮失败回滚后必须补跑 stale 清理（修法 A 第 2 半，2026-10-27）。

背景（ladder-v5-paper 产线实测）：
- `execute_signal` 原来只有 `report.ok` 才跑 `_cancel_stale_owned`（撤旧单）。
  失败轮次走 `_rollback_newly_placed` 撤掉**本轮新单**，但快照里的**旧单**
  （上一轮的阶梯 + SL）永远没人收 —— 只要失败连续出现，每轮泄漏一批
  （实测 20 条入场 + 11 条 SL 堆积）。
- 回滚之后旧单成了纯垃圾（本轮新单已撤，场上只剩上一轮的遗留），
  此时「承接物守卫」必然 no_replacement 拒撤 —— 但**整轮失败回滚**本身就是
  撤旧依据，把快照面全量当显式撤单面传入。真在保护持仓的 reduce-only 单
  仍由 `_should_keep_protection` 拦住，不会裸奔。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor, StepResult  # noqa: E402
from omnialpha.schema import Intent, SignalFile  # noqa: E402


def _exec(pre_owned, capture, *, default_replace="symbol", fail_on=None):
    """最小 Executor：快照固定为 pre_owned，第 fail_on 条 intent 失败。"""
    ex = Executor.__new__(Executor)
    ex.client = None
    ex.symbols_whitelist = None
    ex.max_notional_usd = None
    ex.position_policy = "free"
    ex.default_replace = default_replace
    ex.order_scope = "own"
    ex.require_sl = False
    ex.account_risk = {}
    ex.root = Path(__file__).parent
    ex.bot_id = "t"
    ex.account = ""
    ex.label_prefix = "mg"
    ex.alert_store = None
    ex.place_before_cancel = False

    calls = {"n": 0}

    def _intent_step(i):
        calls["n"] += 1
        if fail_on is not None and calls["n"] == fail_on:
            return StepResult(i.action, i.symbol, False, error="boom")
        return StepResult(i.action, i.symbol, True, detail={})

    ex._execute_intent = _intent_step
    ex._entry_gate = lambda i: None
    ex._snapshot_owned = lambda intents_, mode: pre_owned
    ex._rollback_newly_placed = (
        lambda pre, intents_, report: capture.setdefault("rollback", []).append(
            [i.action for i in intents_])
    )

    def _cancel(pre, intents_, report, explicit_cancel_symbols=None):
        capture["stale_calls"] = capture.get("stale_calls", []) + [
            {"syms": set(explicit_cancel_symbols or ()),
             "pre_symbols": sorted(pre)}]

    ex._cancel_stale_owned = _cancel
    return ex


_OPEN = Intent(action="open_long", symbol="ETH_USDT", price=2526, sl=2330, size_usd=28)
_MODIFY = Intent(action="modify_tp_sl", symbol="ETH_USDT", tp=2529.0)
_PRE = {"ETH_USDT": {"prefix": "mg", "price_ids": {"sl_old"},
                     "order_ids": {f"g{i}" for i in range(1, 11)}}}


class TestFailedRoundCleansOldOrders(unittest.TestCase):
    def test_failed_round_triggers_stale_cleanup(self):
        """★核心回归★ 第 2 条腿失败 → 回滚 + 补跑 stale 清理，
        快照面全量当显式撤单面（否则承接物守卫会拒撤）。"""
        cap = {}
        ex = _exec(_PRE, cap, fail_on=2)
        rep = ex.execute_signal(SignalFile(intents=[_OPEN, _MODIFY], replace="symbol"))
        self.assertFalse(rep.ok)
        self.assertIn("rollback", cap, "失败轮必须先回滚本轮新单")
        self.assertEqual(len(cap.get("stale_calls", [])), 1,
                         "回滚后必须补跑一次 stale 清理")
        call = cap["stale_calls"][0]
        self.assertEqual(call["pre_symbols"], ["ETH_USDT"])
        self.assertEqual(call["syms"], {"ETH_USDT"},
                         "快照 symbol 必须进显式撤单面，绕开 no_replacement 守卫")

    def test_ok_round_unchanged(self):
        """成功轮行为不变：modify-only 轮守卫收到空集合。"""
        cap = {}
        ex = _exec(_PRE, cap)
        rep = ex.execute_signal(SignalFile(intents=[_MODIFY], replace="symbol"))
        self.assertTrue(rep.ok)
        self.assertNotIn("rollback", cap)
        self.assertEqual(len(cap.get("stale_calls", [])), 1)
        self.assertEqual(cap["stale_calls"][0]["syms"], set())

    def test_failed_round_without_replace_no_cleanup(self):
        """replace=none 的失败轮不补跑清理（无 replace 语义就不动旧单）。"""
        cap = {}
        ex = _exec(_PRE, cap, default_replace="none", fail_on=1)
        rep = ex.execute_signal(SignalFile(intents=[_OPEN]))
        self.assertFalse(rep.ok)
        self.assertIn("rollback", cap)
        self.assertEqual(cap.get("stale_calls", []), [])

    def test_failed_round_empty_snapshot_no_cleanup(self):
        """快照为空（首轮）→ 回滚即可，无需清理调用。"""
        cap = {}
        ex = _exec({}, cap, fail_on=1)
        rep = ex.execute_signal(SignalFile(intents=[_OPEN], replace="symbol"))
        self.assertFalse(rep.ok)
        self.assertIn("rollback", cap)
        self.assertEqual(cap.get("stale_calls", []), [])


if __name__ == "__main__":
    unittest.main()
