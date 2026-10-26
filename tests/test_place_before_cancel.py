"""place_before_cancel（P1 空窗修复）与 _cap_combined_risk（D 组合风险闸门）回归测试。

背景（martin-grid 产线实测）：
- 显式 cancel_all 按优先级排最前先执行 → 「撤完旧的、新的还没挂上」的交易所空窗
  （撤单→重挂之间 ~4 分钟无本 bot 挂单）。place_before_cancel 开启时撤类 intent
  转换为「快照差集」语义：新单挂完后由 _cancel_stale_owned 撤旧单差集。
- 单档 _align_size_to_risk 挡不住「每档合规、10 档加起来爆表」（满阶止损 ≈40% 权益）。
  total_risk_pct 组合闸门等比缩小全部 open 档，触发必须打 WARNING（P0 盲区教训）。

修复分层：两者都是**引擎级**（所有 bot 可用），但由逐 bot 配置开启——
未配置的 bot 行为零变化（默认关）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent, SignalFile  # noqa: E402


def _intent(action, symbol="ETH_USDT", **kw):
    return Intent(action=action, symbol=symbol, **kw)


class _StubAccount:
    def __init__(self, total):
        self._total = total

    def get_account(self):
        return {"total": self._total}


class _ExplodingAccount:
    def get_account(self):
        raise RuntimeError("api down")


def _exec(place_before_cancel=False, total_risk_pct=None, equity=72.0,
          client=None) -> Executor:
    """execute_signal / _cap_combined_risk 所需的最小属性面。

    _execute_intent / _snapshot_owned / _cancel_stale_owned 由测试用例
    patch —— 关注的是编排顺序与转换语义，不测下游下单。
    """
    ex = Executor.__new__(Executor)
    ex.client = client if client is not None else _StubAccount(equity)
    ex.symbols_whitelist = None
    ex.max_notional_usd = None
    ex.position_policy = "free"
    ex.default_replace = "none"
    ex.order_scope = "own"
    ex.require_sl = False
    ar = {}
    if total_risk_pct is not None:
        ar["total_risk_pct"] = total_risk_pct
    ex.account_risk = ar
    ex.root = Path(__file__).parent  # _stop_requested 读 root/bots/.../stop，不存在 = False
    ex.bot_id = "t"
    ex.account = ""
    ex.label_prefix = "tst"
    ex.alert_store = None
    ex.place_before_cancel = place_before_cancel
    return ex


class TestPlaceBeforeCancel(unittest.TestCase):
    def _run(self, ex, intents, executed=None, stale=None, snapshot=None):
        """patch 掉下游，跑 execute_signal，返回 report。"""
        executed = executed if executed is not None else []
        stale_calls = stale if stale is not None else []
        ex._execute_intent = lambda i: (
            executed.append(i.action),
            __import__("omnialpha.executor", fromlist=["StepResult"]).StepResult(
                i.action, i.symbol, True, detail={"stub": 1}),
        )[-1]
        ex._entry_gate = lambda i: None
        ex._snapshot_owned = lambda intents_, mode: (snapshot.append(mode), {})[-1] \
            if snapshot is not None else {}
        ex._cancel_stale_owned = lambda pre, intents_, report, explicit_cancel_symbols=None: \
            stale_calls.append([i.action for i in intents_])
        ex._rollback_newly_placed = lambda *a, **k: None
        signal = SignalFile(intents=intents)
        return ex.execute_signal(signal)

    def test_default_cancel_first_keeps_legacy_semantics(self):
        """开关关闭（默认）→ cancel_all 仍最先执行，其他 bot 行为零变化。"""
        ex = _exec(place_before_cancel=False)
        executed = []
        stale = []
        self._run(ex, [
            _intent("open_long", size_usd=28, sl=2345, price=2526),
            _intent("cancel_all"),
        ], executed=executed, stale=stale)
        self.assertEqual(executed[0], "cancel_all")
        self.assertEqual(executed[1], "open_long")
        self.assertEqual(stale, [])          # replace=none → stale 清理不跑

    def test_converted_cancel_runs_after_opens(self):
        """开关开启 → cancel_all 被转换，不再单独执行；stale 清理接管撤旧。"""
        ex = _exec(place_before_cancel=True)
        executed = []
        stale = []
        snap_modes = []
        report = self._run(ex, [
            _intent("open_long", size_usd=28, sl=2345, price=2526),
            _intent("cancel_all"),
        ], executed=executed, stale=stale, snapshot=snap_modes)
        self.assertNotIn("cancel_all", executed)
        self.assertEqual(executed, ["open_long"])
        # replace_mode 提升为 symbol → stale 清理必须跑（撤旧差集）
        self.assertEqual(stale, [["open_long"]])
        # 审计留痕：转换必须可见
        conv = [r for r in report.results if (r.detail or {}).get("converted")]
        self.assertEqual(len(conv), 1)
        self.assertEqual(conv[0].action, "cancel_all")
        self.assertEqual(conv[0].detail["deferred_to"], "stale_cleanup")

    def test_pure_cancel_round_not_converted(self):
        """纯撤轮（只有 cancel_all，无 open）→ 不转换，原语义直接撤。"""
        ex = _exec(place_before_cancel=True)
        executed = []
        stale = []
        self._run(ex, [_intent("cancel_all")], executed=executed, stale=stale)
        self.assertEqual(executed, ["cancel_all"])
        self.assertEqual(stale, [])

    def test_open_failure_leaves_old_orders_intact(self):
        """开启开关 + 新单挂失败 → 整轮 fail，旧单不被撤（比先撤后挂安全）。"""
        ex = _exec(place_before_cancel=True)
        from omnialpha.executor import StepResult
        executed = []
        stale = []

        def _fail(i):
            executed.append(i.action)
            return StepResult(i.action, i.symbol, False, error="stub fail")

        ex._execute_intent = _fail
        ex._entry_gate = lambda i: None
        ex._snapshot_owned = lambda intents_, mode: {}
        ex._cancel_stale_owned = lambda pre, intents_, report, explicit_cancel_symbols=None: stale.append(1)
        ex._rollback_newly_placed = lambda *a, **k: None
        signal = SignalFile(intents=[
            _intent("open_long", size_usd=28, sl=2345, price=2526),
            _intent("cancel_all"),
        ])
        report = ex.execute_signal(signal)
        self.assertFalse(report.ok)
        self.assertEqual(stale, [])          # 失败轮不撤旧单


class TestCombinedRiskCap(unittest.TestCase):
    def test_over_limit_scales_all_open_legs_and_logs(self):
        """满阶 29 张 ≈ 41U 止损 > 72×0.35=25.2U → 等比缩 + WARNING(RISK_BUDGET)。"""
        ex = _exec(total_risk_pct=0.35, equity=72.0)
        intents = [
            _intent("open_long", size_usd=28.0, sl=2345.0, price=2526.0),   # dist 7.2% → 2.02U
            _intent("open_long", size_usd=160.0, sl=2345.0, price=2495.0),  # dist 6.0% → 9.6U
            _intent("open_long", size_usd=186.0, sl=2345.0, price=2441.0),  # dist 3.9% → 7.3U
            _intent("open_long", size_usd=239.0, sl=2345.0, price=2405.0),  # dist 2.5% → 5.98U
            _intent("hold"),
        ]
        # 再加 7 个网格档凑满阶形态
        for px in (2546, 2531, 2518, 2492, 2475, 2440, 2423):
            intents.insert(-1, _intent("open_long", size_usd=28.0, sl=2345.0, price=px))
        total = sum(abs(i.price - i.sl) / i.price * i.size_usd
                    for i in intents if i.action == "open_long")
        self.assertGreater(total, 72.0 * 0.35, "测试前提：满阶止损应超限")
        # ⚠️ 闸门是原地改写（省一次对象拷贝），before 必须在调用前快照
        before_sizes = {id(i): i.size_usd for i in intents if i.action == "open_long"}
        out, note = ex._cap_combined_risk(intents)
        self.assertTrue(note)   # 缩仓必须带 note（WARNING 由 execute_signal 层打，见下一测试）
        scale = (72.0 * 0.35) / total
        for i in (x for x in out if x.action == "open_long"):
            self.assertAlmostEqual(before_sizes[id(i)] * scale, i.size_usd, places=1)
        hold_intents = [i for i in out if i.action == "hold"]
        self.assertEqual(len(hold_intents), 1)   # 非 open 档不动

    def test_execute_signal_logs_risk_budget(self):
        """execute_signal 层：组合闸门触发必须打 WARNING(RISK_BUDGET)——P0 盲区教训。"""
        ex = _exec(place_before_cancel=True, total_risk_pct=0.35, equity=72.0)
        intents = [
            _intent("open_long", size_usd=160.0, sl=2345.0, price=2495.0),
            _intent("open_long", size_usd=239.0, sl=2345.0, price=2405.0),
            _intent("cancel_all"),
        ]
        # dist 6.0%×160 + 2.5%×239 = 9.6 + 6.0 = 15.6 < 25.2 不超限 → 加大
        intents[0] = _intent("open_long", size_usd=300.0, sl=2345.0, price=2495.0)
        # 300×6.0% + 239×2.5% = 18 + 6 = 24 仍差一点 → 再加大第一档
        intents[0] = _intent("open_long", size_usd=380.0, sl=2345.0, price=2495.0)
        from omnialpha.executor import StepResult
        ex._execute_intent = lambda i: StepResult(i.action, i.symbol, True, detail={})
        ex._entry_gate = lambda i: None
        ex._snapshot_owned = lambda intents_, mode: {}
        ex._cancel_stale_owned = lambda pre, intents_, report, explicit_cancel_symbols=None: None
        ex._rollback_newly_placed = lambda *a, **k: None
        with self.assertLogs("omnialpha.executor", level="WARNING") as cm:
            report = ex.execute_signal(SignalFile(intents=intents))
        self.assertTrue(any("RISK_BUDGET" in m for m in cm.output), cm.output)

    def test_under_limit_untouched(self):
        ex = _exec(total_risk_pct=0.35, equity=72.0)
        intents = [_intent("open_long", size_usd=28.0, sl=2345.0, price=2526.0)]
        with self.assertNoLogs("omnialpha.executor", level="WARNING"):
            out, note = ex._cap_combined_risk(intents)
        self.assertEqual(note, "")
        self.assertAlmostEqual(out[0].size_usd, 28.0)

    def test_disabled_by_default(self):
        """未配置 total_risk_pct → 闸门完全不动（其他 bot 零影响）。"""
        ex = _exec(equity=72.0)   # account_risk 无 total_risk_pct
        intents = [_intent("open_long", size_usd=500.0, sl=2345.0, price=2526.0)]
        out, note = ex._cap_combined_risk(intents)
        self.assertEqual(note, "")
        self.assertAlmostEqual(out[0].size_usd, 500.0)

    def test_fail_open_on_account_error(self):
        """权益查询失败 → 原样返回（闸门只缩仓不拒单，不能卡死下单链路）。"""
        ex = _exec(total_risk_pct=0.35, client=_ExplodingAccount())
        intents = [_intent("open_long", size_usd=500.0, sl=2345.0, price=2526.0)]
        out, note = ex._cap_combined_risk(intents)
        self.assertEqual(note, "")
        self.assertAlmostEqual(out[0].size_usd, 500.0)


if __name__ == "__main__":
    unittest.main()
