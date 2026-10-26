"""replace「空网格」修复（系统级）回归测试。

背景（martin-grid 产线实测 2026-10-14）：
- `bridge.chips_to_signal` 曾对**每个** LLM 计划无条件打 `replace: symbol`。
  引擎据此在本轮新单挂完后，撤掉该 symbol 上「快照里有、新计划里没有」的旧 owned
  单。当本轮计划**只有** modify_tp_sl / reduce（没有任何新挂入场单）时，「新计划
  里的单」为空 → 整个阶梯被当陈旧单撤光且无替补 → 一个轮次节拍（实测 15~21min）
  的网格空窗。产线 2 天出现 16 次。盘面即「只剩止盈止损、没有网格」。

修复分两层（都是**引擎级**，对所有 bot 生效，非单 bot 配置调整）：
  B（契约层，bridge.chips_to_signal）：仅当本轮含入场动作（open/add/stop_entry）时
     才用 `replace=symbol`，否则 `none`。改保护不依赖 replace —— modify_tp_sl 自身
     会清理被替换那一类的旧保护单。
  A（执行层，Executor._cancel_stale_owned）：撤旧**普通单**前要求「承接物」—— 本轮为该
     symbol 挂出**新普通单**（阶梯/入场挂单）**或** 有显式撤单意图；否则跳过。覆盖
     `default_replace: symbol` 等配置型触发路径，与 B 互为纵深。

⚠️ A 的承接物**只认普通单**：管理轮（modify_tp_sl）**必然**会挂一条新 TP，若把条件单
   也算承接物，守卫永不触发（初版即踩此坑：`placed_new` 含 `cur_price` → 实测本轮挂
   新 TP → 阶梯照撤）。`test_new_tp_only_still_skips` 锁死这条。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import ExecReport, Executor  # noqa: E402
from omnialpha.schema import Intent, SignalFile  # noqa: E402
from omnialpha.strategist.bridge import chips_to_signal  # noqa: E402
from omnialpha.strategist.risk import RiskResult  # noqa: E402
from omnialpha.strategist.schema import Chip, Plan  # noqa: E402


# ────────────────────────── B：bridge 条件化 replace ──────────────────────────

def _bridge(chips, cycle="c1"):
    plan = Plan(cycle_id=cycle, reasoning="r", chips=list(chips))
    return chips_to_signal(plan, RiskResult(accepted=list(chips)), bot_id="mg")


class TestBridgeReplaceConditional(unittest.TestCase):
    def test_pure_modify_round_uses_none(self):
        """本轮只有 modify_tp_sl（改保护）→ replace=none，旧阶梯不该被撤。"""
        out = _bridge([Chip(symbol="ETH_USDT", action="modify_tp_sl", tp=2529.0)])
        self.assertEqual(out["replace"], "none")
        self.assertEqual([o["action"] for o in out["orders"]], ["modify_tp_sl"])

    def test_hold_with_tp_becomes_modify_and_none(self):
        """hold 携带 tp/sl（bridge 转成 modify_tp_sl）→ 同样不该触发 replace。"""
        out = _bridge([Chip(symbol="ETH_USDT", action="hold", sl=2330.0)])
        self.assertEqual(out["replace"], "none")
        self.assertEqual(out["orders"][0]["action"], "modify_tp_sl")

    def test_pure_reduce_round_uses_none(self):
        """纯减仓（reduce）→ 无入场单 → replace=none。"""
        out = _bridge([Chip(symbol="ETH_USDT", action="reduce_long", size=1)])
        self.assertEqual(out["replace"], "none")

    def test_open_long_uses_symbol(self):
        """铺/加入场单 → replace=symbol（新梯接替旧梯）。"""
        out = _bridge([Chip(symbol="ETH_USDT", action="open_long",
                            size_usd=28, sl=2330, price=2526)])
        self.assertEqual(out["replace"], "symbol")

    def test_add_and_stop_entry_use_symbol(self):
        """add_* / stop_entry_* 都算入场动作 → replace=symbol。"""
        for act in ("add_long", "add_short", "stop_entry_long", "stop_entry_short",
                    "open_short"):
            out = _bridge([Chip(symbol="ETH_USDT", action=act, price=2526)])
            self.assertEqual(out["replace"], "symbol", act)

    def test_modify_plus_open_uses_symbol(self):
        """混合轮：只要有入场单就 replace=symbol（新梯覆盖旧梯）。"""
        out = _bridge([
            Chip(symbol="ETH_USDT", action="modify_tp_sl", tp=2529.0),
            Chip(symbol="ETH_USDT", action="open_long", size_usd=28, price=2526, sl=2330),
        ])
        self.assertEqual(out["replace"], "symbol")

    def test_empty_orders_uses_none(self):
        """无 orders（理论上会被写成 hold 审计文件）→ replace 不得为 symbol。"""
        out = _bridge([])
        self.assertEqual(out["replace"], "none")


# ────────────────────── A：_cancel_stale_owned 承接物守卫 ──────────────────────

class _FakeClient:
    def __init__(self):
        self.cancelled_price = []
        self.cancelled_order = []

    def cancel_price_order(self, pid):
        self.cancelled_price.append(pid)

    def cancel_order(self, oid):
        self.cancelled_order.append(oid)


def _cancel_exec(cur_price, cur_orders, *, positions=None, pending_entry=False):
    """构造只面向 _cancel_stale_owned 的最小 Executor（下游全桩）。"""
    ex = Executor.__new__(Executor)
    ex.order_scope = "own"
    ex.label_prefix = "mtg"
    ex.client = _FakeClient()
    ex._own_prefix = lambda label="": "mtg"
    ex._order_id = lambda p: str(p.get("id") or "")
    ex._owned_price_orders = lambda sym, prefix: list(cur_price)
    ex._owned_open_orders = lambda sym, prefix: list(cur_orders)
    ex._positions_or_error = lambda: (positions if positions is not None else [], "")
    ex._has_pending_entry = lambda sym: pending_entry
    ex._should_keep_protection = lambda o, pos, pe: False
    return ex


def _snap(order_ids=(), price_ids=()):
    return {"ETH_USDT": {"prefix": "mtg",
                         "price_ids": set(price_ids),
                         "order_ids": set(order_ids)}}


_MODIFY = [Intent(action="modify_tp_sl", symbol="ETH_USDT")]


class TestNoReplacementGuard(unittest.TestCase):
    def test_no_new_order_no_cancel_skips(self):
        """本轮无新单、无显式撤单 → 旧阶梯保留（不再被撤光）—— 核心回归。"""
        ex = _cancel_exec([], [{"id": "o1"}, {"id": "o2"}])
        report = ExecReport()
        ex._cancel_stale_owned(_snap(order_ids=("o1", "o2")), _MODIFY, report,
                               explicit_cancel_symbols=set())
        self.assertEqual(ex.client.cancelled_order, [])
        skipped = [r for r in report.results if (r.detail or {}).get("skipped") == "no_replacement"]
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0].ok)

    def test_new_tp_only_still_skips(self):
        """★真实产线场景★：管理轮（modify_tp_sl）本轮**挂了新 TP**（价格单），
        但没挂任何新普通单 → 阶梯必须保留。

        这是原实现（placed_new 含价格单）的漏网之鱼：管理轮**必然**挂一条新 TP，
        把它算作承接物 → 守卫永不触发 → 10 张阶梯照撤、空窗照旧（实测 2026-10-14）。"""
        # 快照：1 条旧 TP + 10 张阶梯；当前：新 TP（旧 TP 已被 modify_tp_sl 撤）+ 原 10 张阶梯
        ex = _cancel_exec([{"id": "tp_new"}], [{"id": f"g{i}"} for i in range(1, 11)])
        report = ExecReport()
        ex._cancel_stale_owned(
            _snap(order_ids=tuple(f"g{i}" for i in range(1, 11)), price_ids=("tp_old",)),
            _MODIFY, report, explicit_cancel_symbols=set())
        self.assertEqual(ex.client.cancelled_order, [],
                         "管理轮挂新 TP 不能算承接物，阶梯必须保留")
        self.assertTrue(any((r.detail or {}).get("skipped") == "no_replacement"
                            for r in report.results))

    def test_new_regular_orders_allow_cancel_even_with_new_tp(self):
        """铺单轮：挂了新普通单（阶梯）→ 即便同时有新 TP 也照常撤旧阶梯。"""
        ex = _cancel_exec(
            [{"id": "tp_new"}],
            [{"id": f"g{i}"} for i in range(1, 11)] + [{"id": f"n{i}"} for i in range(1, 11)],
        )
        report = ExecReport()
        ex._cancel_stale_owned(
            _snap(order_ids=tuple(f"g{i}" for i in range(1, 11)), price_ids=("tp_old",)),
            _MODIFY, report, explicit_cancel_symbols=set())
        self.assertEqual(sorted(ex.client.cancelled_order),
                         sorted(f"g{i}" for i in range(1, 11)))

    def test_new_order_present_cancels_old(self):
        """本轮挂出新单（当前 id 多于快照）→ 照常撤旧单差集。"""
        ex = _cancel_exec([], [{"id": "o1"}, {"id": "o2"}])
        report = ExecReport()
        ex._cancel_stale_owned(_snap(order_ids=("o1",)), _MODIFY, report,
                               explicit_cancel_symbols=set())
        self.assertEqual(ex.client.cancelled_order, ["o1"])

    def test_explicit_cancel_intent_cancels_old(self):
        """本轮对该 symbol 有显式撤单意图 → 即便没新单也照撤。"""
        ex = _cancel_exec([], [{"id": "o1"}])
        report = ExecReport()
        ex._cancel_stale_owned(_snap(order_ids=("o1",)), _MODIFY, report,
                               explicit_cancel_symbols={"ETH_USDT"})
        self.assertEqual(ex.client.cancelled_order, ["o1"])

    def test_wide_explicit_cancel_cancels_all(self):
        """全合约撤单意图（symbol=""）→ 对所有 symbol 生效。"""
        ex = _cancel_exec([], [{"id": "o1"}])
        report = ExecReport()
        ex._cancel_stale_owned(_snap(order_ids=("o1",)), _MODIFY, report,
                               explicit_cancel_symbols={""})
        self.assertEqual(ex.client.cancelled_order, ["o1"])

    def test_hold_only_round_keeps_legacy_cancel(self):
        """纯 hold 轮不触发守卫 → replace 保持既有「清陈旧单」语义（不误伤作用域契约）。"""
        ex = _cancel_exec([], [{"id": "o1"}])
        report = ExecReport()
        ex._cancel_stale_owned(_snap(order_ids=("o1",)),
                               [Intent(action="hold", symbol="ETH_USDT")], report,
                               explicit_cancel_symbols=set())
        self.assertEqual(ex.client.cancelled_order, ["o1"])

    def test_empty_intents_keeps_legacy_cancel(self):
        """空 intent 轮（replace 显式清单语义）→ 不触发守卫，照撤。"""
        ex = _cancel_exec([], [{"id": "o1"}])
        report = ExecReport()
        ex._cancel_stale_owned(_snap(order_ids=("o1",)), [], report,
                               explicit_cancel_symbols=set())
        self.assertEqual(ex.client.cancelled_order, ["o1"])

    def test_default_none_guard_is_off(self):
        """不传 explicit_cancel_symbols（默认 None）→ 退化为「无显式撤单」，
        无新单时仍然跳过 —— 守卫方向始终是 fail-closed 的不撤。"""
        ex = _cancel_exec([], [{"id": "o1"}])
        report = ExecReport()
        ex._cancel_stale_owned(_snap(order_ids=("o1",)), _MODIFY, report)
        self.assertEqual(ex.client.cancelled_order, [])
        self.assertTrue(any((r.detail or {}).get("skipped") == "no_replacement"
                            for r in report.results))


# ─────────────── 集成：execute_signal 把显式撤单 symbol 传给守卫 ───────────────

def _exec_signal(place_before_cancel=False, default_replace="none", symbol_whitelist=None,
                 label_prefix="mg", capture=None):
    ex = Executor.__new__(Executor)
    ex.client = None
    ex.symbols_whitelist = symbol_whitelist
    ex.max_notional_usd = None
    ex.position_policy = "free"
    ex.default_replace = default_replace
    ex.order_scope = "own"
    ex.require_sl = False
    ex.account_risk = {}
    ex.root = Path(__file__).parent
    ex.bot_id = "t"
    ex.account = ""
    ex.label_prefix = label_prefix
    ex.alert_store = None
    ex.place_before_cancel = place_before_cancel
    from omnialpha.executor import StepResult
    ex._execute_intent = lambda i: StepResult(i.action, i.symbol, True, detail={})
    ex._entry_gate = lambda i: None
    ex._snapshot_owned = lambda intents_, mode: {}
    ex._rollback_newly_placed = lambda *a, **k: None
    ex._cancel_stale_owned = (
        lambda pre, intents_, report, explicit_cancel_symbols=None: capture.update(
            syms=explicit_cancel_symbols, intents=[i.action for i in intents_])
    )
    return ex


class TestExecuteSignalWiresCancelSyms(unittest.TestCase):
    def test_pure_modify_with_signal_replace_passes_empty(self):
        """信号层 replace=symbol（模拟配置 default_replace / 旧 bridge 产物）
        + 只有 modify_tp_sl → 守卫收到空集合（无显式撤单）→ 会跳过撤旧。"""
        cap = {}
        ex = _exec_signal(default_replace="symbol", capture=cap)
        ex.execute_signal(SignalFile(
            intents=[Intent(action="modify_tp_sl", symbol="ETH_USDT")],
            replace="symbol",
        ))
        self.assertEqual(cap["syms"], set())
        self.assertEqual(cap["intents"], ["modify_tp_sl"])

    def test_converted_cancel_passes_its_symbol(self):
        """cancel_all 被 place_before_cancel 转换 → 其 symbol 必须下传给守卫，
        否则显式撤单会被「无承接物」守卫误杀。"""
        cap = {}
        ex = _exec_signal(place_before_cancel=True, default_replace="symbol", capture=cap)
        ex.execute_signal(SignalFile(
            intents=[Intent(action="open_long", symbol="ETH_USDT", price=2526, sl=2330),
                     Intent(action="cancel_all", symbol="ETH_USDT")],
            replace="symbol",
        ))
        self.assertEqual(cap["syms"], {"ETH_USDT"})


# ───────── 集成：真实 paper 交易所，走完整 execute_signal（离线 stub feed）─────────

import tempfile  # noqa: E402

from omnialpha.paper.exchange import PaperExchange  # noqa: E402

_SYM = "BTC_USDT"


class _Meta:
    quanto_multiplier = 1.0
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class _Feed:
    name = "fake"

    def __init__(self, last=100.0):
        self.last = float(last)
        self.bid, self.ask = self.last - 0.1, self.last + 0.1

    def get_last_price(self, symbol):
        return self.last

    def get_orderbook_top(self, symbol, limit=5):
        return {"bids": [{"p": self.bid, "s": 100}], "asks": [{"p": self.ask, "s": 100}]}

    def get_ticker(self, symbol):
        return {"last": self.last, "mark_price": self.last, "funding_rate": 0.0001}

    def get_contract(self, symbol):
        return _Meta()

    def get_klines(self, symbol, interval, limit=100):
        return []

    def get_contract_stats(self, symbol, limit=1):
        return []


class TestGridSurvivesManagementRound(unittest.TestCase):
    """复现产线失败序列并验证修复：

      R1 铺网格：10 张限价买单（`t-mtg`）+ 一个多头持仓
      R2 管理轮：`replace=symbol`（旧 bridge 产物 / 配置型）+ 仅 `modify_tp_sl`
      → 网格必须**原样保留**（修复前：10 张全被当陈旧单撤光 → 只剩 TP 的空窗）。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 清理顺序（LIFO）：先关 store（释放 sqlite 文件锁），再删临时目录 ——
        # 否则 Windows 上 db 仍被占用，TemporaryDirectory 删不掉会留 tmp* 残留。
        self.addCleanup(self._td.cleanup)
        tmp = Path(self._td.name)
        self.feed = _Feed(100.0)
        self.ex = PaperExchange(env="paper", store_path=tmp / "a.db", feed=self.feed,
                                config={"initial_capital": 10000, "leverage": 20})
        self.addCleanup(self.ex.store.close)
        self.executor = Executor(self.ex, label_prefix="mtg", root=tmp, bot_id="mg")

    def _grid(self, n=10):
        # 价格落在 paper 校验带内（现价 100 → [95,105]）
        ids = []
        for i in range(n):
            o = self.ex.place_order({"contract": _SYM, "size": 1, "type": "limit",
                                     "price": str(99.0 - i * 0.3), "text": "t-mtg"})
            ids.append(str(o["id"]))
        return ids

    def _grid_ids(self):
        return sorted(str(o["id"]) for o in self.ex.list_orders(_SYM)
                      if str(o.get("text") or "") == "t-mtg")

    def test_signal_replace_with_modify_only_keeps_grid(self):
        grid = self._grid()
        # 一个多头持仓（modify_tp_sl 的对象）
        self.ex.place_order({"contract": _SYM, "size": 1, "type": "market",
                             "text": "t-mtg"})
        self.assertEqual(len(self._grid_ids()), 10, "R1 铺单失败")

        # R2：旧 bridge 会产出 replace=symbol + 仅 modify_tp_sl
        report = self.executor.execute_signal(SignalFile(
            intents=[Intent(action="modify_tp_sl", symbol=_SYM, tp=110.0, sl=90.0)],
            replace="symbol",
        ))
        self.assertTrue(report.ok, report)
        self.assertEqual(self._grid_ids(), sorted(grid),
                         "管理轮不得撤掉网格（修复前这里会变成 []）")

    def test_open_round_still_replaces_grid(self):
        """对照：铺单轮（含 open_long）→ 旧网格照常被 replace 掉（新梯接替旧梯）。"""
        grid = self._grid()
        report = self.executor.execute_signal(SignalFile(
            intents=[Intent(action="open_long", symbol=_SYM, price=96.0, sl=90.0,
                            size_usd=200.0, order_type="limit", leverage=20,
                            trigger_rule_tp=1, trigger_rule_sl=2)],
            replace="symbol",
        ))
        self.assertTrue(report.ok, report)
        after = set(self._grid_ids())
        self.assertFalse(set(grid) <= after,
                         "铺单轮应撤掉旧梯（新梯接替）")


if __name__ == "__main__":
    unittest.main()
