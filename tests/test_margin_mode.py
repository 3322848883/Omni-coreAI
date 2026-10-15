# -*- coding: utf-8 -*-
"""保证金模式（逐仓/全仓）配置化 + 程序幂等 ensure（2026-10-16）。

**背景**：GRID 账户一直是**逐仓**（实测 `pos_margin_mode: isolated`），而人格
`prompts/ladder_nogate.md:29` 明明写着 `margin_mode: cross` —— 因为 live 侧
**从来没有配置入口**（只有 `paper.margin_mode`，那是纸面引擎），唯一入口是 AI
chip 的 `intent.margin_mode`，而人格字段表与 chip 契约都没有该字段 → 从未写过
→ 从未生效。「配了但没人读」的典型。

**修复**：`account_risk.margin_mode`（cross|isolated）作为 live 侧配置入口；
`Executor._ensure_margin_mode` 幂等 ensure（AI 显式给 > bot 配置）。

**关键约束（实测）**：Gate 只允许「该合约**无持仓、无挂单**」时切换 ——
2026-10-16 盘上 10 档挂单时调 `dual_comp/positions/cross_mode` 报
`ORDER_PENDING`。所以失败是**预期路径**（阶梯常态有挂单），必须静默 + 不阻塞
下单，等重铺空窗自动生效。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.executor import Executor
from omnialpha.gate_client import ContractMeta
from omnialpha.schema import Intent, infer_trigger_rules


META = ContractMeta(name="ETH_USDT", quanto_multiplier=0.01,
                    order_size_round=1, order_price_round=0.05, leverage_max=50)


class _Client:
    """桩客户端：记录 set_margin_mode 调用；可配置抛错模拟 ORDER_PENDING。"""

    def __init__(self, raise_on_margin=None):
        self.margin_calls = []
        self.leverage_calls = []
        self._raise = raise_on_margin

    def set_margin_mode(self, symbol, mode):
        self.margin_calls.append((symbol, mode))
        if self._raise is not None:
            raise self._raise
        return [{"contract": symbol, "pos_margin_mode": mode}]

    def set_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))
        return {}

    def get_positions(self):
        return []

    def list_price_orders(self, symbol):
        return []

    def list_orders(self, symbol):
        return []

    def get_contract(self, symbol):
        return META

    def get_last_price(self, symbol):
        return 2300.0


def _ex(tmp, client, account_risk=None, **kw):
    return Executor(client, label_prefix="mtg", root=tmp, bot_id="martin-grid",
                    account_risk=account_risk or {}, **kw)


class TestEnsureMarginMode(unittest.TestCase):
    """`_ensure_margin_mode` 单元：幂等短路 / 失败静默 / 非法值不动。"""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)

    def test_switch_cross(self):
        cli = _Client()
        ex = _ex(self.tmp, cli)
        self.assertEqual("switched", ex._ensure_margin_mode("ETH_USDT", "cross"))
        self.assertEqual([("ETH_USDT", "cross")], cli.margin_calls)

    def test_second_call_is_cached(self):
        """成功一次后短路 —— 不每轮白跑一次 API。"""
        cli = _Client()
        ex = _ex(self.tmp, cli)
        ex._ensure_margin_mode("ETH_USDT", "cross")
        self.assertEqual("cached", ex._ensure_margin_mode("ETH_USDT", "cross"))
        self.assertEqual(1, len(cli.margin_calls))

    def test_case_and_space_tolerant(self):
        cli = _Client()
        ex = _ex(self.tmp, cli)
        self.assertEqual("switched", ex._ensure_margin_mode("ETH_USDT", " CROSS "))
        self.assertEqual([("ETH_USDT", "cross")], cli.margin_calls)

    def test_order_pending_is_silent_and_not_fatal(self):
        """有挂单/持仓 → Gate 报 ORDER_PENDING → 返回 pending，绝不抛。"""
        cli = _Client(raise_on_margin=RuntimeError("ORDER_PENDING"))
        ex = _ex(self.tmp, cli)
        self.assertEqual("pending", ex._ensure_margin_mode("ETH_USDT", "cross"))
        # 失败不写短路标记 → 下次（空窗）还会再试
        self.assertIsNone(ex._mm_done)

    def test_retry_after_failure_succeeds(self):
        """先失败（有挂单）→ 节流窗口过后重试 → 成功 → 短路生效。"""
        cli = _Client(raise_on_margin=RuntimeError("ORDER_PENDING"))
        ex = _ex(self.tmp, cli)
        self.assertEqual("pending", ex._ensure_margin_mode("ETH_USDT", "cross"))
        # 节流窗口内不重试（`_open` 一轮被调 N 次，不能 N 次都打 API）
        self.assertEqual("throttled", ex._ensure_margin_mode("ETH_USDT", "cross"))
        self.assertEqual(1, len(cli.margin_calls))
        # 空窗到了（节流过期）
        ex._mm_last_try = 0.0
        cli._raise = None
        self.assertEqual("switched", ex._ensure_margin_mode("ETH_USDT", "cross"))
        self.assertEqual("cached", ex._ensure_margin_mode("ETH_USDT", "cross"))
        self.assertEqual(2, len(cli.margin_calls))

    def test_throttle_does_not_leak_to_other_symbol(self):
        """节流按 (symbol, want) 记账 —— 换 symbol 不受影响。"""
        cli = _Client(raise_on_margin=RuntimeError("ORDER_PENDING"))
        ex = _ex(self.tmp, cli)
        ex._ensure_margin_mode("ETH_USDT", "cross")
        self.assertEqual("pending", ex._ensure_margin_mode("BTC_USDT", "cross"))
        self.assertEqual(2, len(cli.margin_calls))

    def test_invalid_value_does_nothing(self):
        cli = _Client()
        ex = _ex(self.tmp, cli)
        for bad in ("", None, "bogus", "  "):
            self.assertEqual("", ex._ensure_margin_mode("ETH_USDT", bad))
        self.assertEqual([], cli.margin_calls)

    def test_isolated_also_supported(self):
        """配置可切回逐仓（双向可配，不写死 cross）。"""
        cli = _Client()
        ex = _ex(self.tmp, cli)
        self.assertEqual("switched", ex._ensure_margin_mode("ETH_USDT", "isolated"))
        self.assertEqual([("ETH_USDT", "isolated")], cli.margin_calls)


class TestOpenAppliesMarginMode(unittest.TestCase):
    """`_open` 集成：AI 显式 > bot 配置 > 都不给则不动。"""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)

    def _run_open(self, client, intent, account_risk=None):
        ex = _ex(self.tmp, client, account_risk=account_risk)
        patches = [
            mock.patch.object(Executor, "_check_symbol", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_safe_mode", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_no_flip", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_open_sl", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_account_risk", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_notional", lambda *a, **k: None),
            mock.patch.object(Executor, "_precheck_exit_triggers", lambda *a, **k: ""),
            mock.patch.object(Executor, "_place_entry_leg",
                              lambda *a, **k: ({"order": {"id": "e1"}}, None)),
            mock.patch.object(Executor, "_place_exit_leg",
                              lambda self, placer, kind, **kw: ({"order": {"id": "x"}}, None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ex._open(intent)

    @staticmethod
    def _intent(size=1, margin_mode=None, leverage=None):
        it = Intent(action="open_long", symbol="ETH_USDT", size=size,
                    sl=2325.0, label="mtg")
        it.trigger_rule_sl = infer_trigger_rules("open_long", False)
        it.margin_mode = margin_mode
        it.leverage = leverage
        return it

    def test_config_drives_switch(self):
        """bot 配置 margin_mode: cross → 开仓前 ensure 一次，detail 留痕。"""
        cli = _Client()
        res = self._run_open(cli, self._intent(), account_risk={"margin_mode": "cross"})
        self.assertTrue(res.ok, res.error)
        self.assertEqual([("ETH_USDT", "cross")], cli.margin_calls)
        self.assertEqual("switched", res.detail.get("margin_mode"))

    def test_intent_overrides_config(self):
        """AI 显式给 isolated → 覆盖配置的 cross（AI 优先）。"""
        cli = _Client()
        res = self._run_open(cli, self._intent(margin_mode="isolated"),
                             account_risk={"margin_mode": "cross"})
        self.assertTrue(res.ok, res.error)
        self.assertEqual([("ETH_USDT", "isolated")], cli.margin_calls)

    def test_no_config_no_call(self):
        """都没给 → 一个 API 都不发（不打扰交易所当前设置）。"""
        cli = _Client()
        res = self._run_open(cli, self._intent(), account_risk={})
        self.assertTrue(res.ok, res.error)
        self.assertEqual([], cli.margin_calls)
        self.assertNotIn("margin_mode", res.detail)

    def test_cross_skips_set_leverage(self):
        """⚠️ 全仓下 leverage 恒为 0（由 cross_leverage_limit 决定）→ **不调** set_leverage。

        否则 Gate 视为「修改杠杆」→ 有挂单时报 ORDER_PENDING → 阶梯第 2 档起
        整轮 rollback（2026-10-16 18:49 实盘实测）。
        """
        cli = _Client()
        res = self._run_open(cli, self._intent(leverage=50),
                             account_risk={"margin_mode": "cross"})
        self.assertTrue(res.ok, res.error)
        self.assertEqual([("ETH_USDT", "cross")], cli.margin_calls)
        self.assertEqual([], cli.leverage_calls)

    def test_isolated_still_sets_leverage(self):
        """逐仓（或未配）→ 照旧设置杠杆，行为不变。"""
        cli = _Client()
        res = self._run_open(cli, self._intent(leverage=50),
                             account_risk={"margin_mode": "isolated"})
        self.assertTrue(res.ok, res.error)
        self.assertEqual([("ETH_USDT", 50)], cli.leverage_calls)

    def test_no_config_still_sets_leverage(self):
        """未配 margin_mode（默认）→ 杠杆照设（老行为零变化）。"""
        cli = _Client()
        res = self._run_open(cli, self._intent(leverage=50), account_risk={})
        self.assertTrue(res.ok, res.error)
        self.assertEqual([("ETH_USDT", 50)], cli.leverage_calls)

    def test_intent_isolated_overrides_cross_for_leverage(self):
        """AI 显式 isolated 覆盖配置 cross → 杠杆要设（回到逐仓语义）。"""
        cli = _Client()
        res = self._run_open(cli, self._intent(leverage=50, margin_mode="isolated"),
                             account_risk={"margin_mode": "cross"})
        self.assertTrue(res.ok, res.error)
        self.assertEqual([("ETH_USDT", 50)], cli.leverage_calls)

    def test_pending_does_not_block_open(self):
        """有挂单切不动 → 下单照常成功，detail 记 pending（不阻塞、不刷屏）。"""
        cli = _Client(raise_on_margin=RuntimeError("ORDER_PENDING"))
        res = self._run_open(cli, self._intent(), account_risk={"margin_mode": "cross"})
        self.assertTrue(res.ok, res.error)
        self.assertEqual("pending", res.detail.get("margin_mode"))

    def test_cached_not_recorded(self):
        """已确认过（cached）→ 不写 detail（避免每轮噪声）。"""
        cli = _Client()
        ex = _ex(self.tmp, cli, account_risk={"margin_mode": "cross"})
        ex._mm_done = ("ETH_USDT", "cross")
        patches = [
            mock.patch.object(Executor, "_check_symbol", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_safe_mode", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_no_flip", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_open_sl", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_account_risk", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_notional", lambda *a, **k: None),
            mock.patch.object(Executor, "_precheck_exit_triggers", lambda *a, **k: ""),
            mock.patch.object(Executor, "_place_entry_leg",
                              lambda *a, **k: ({"order": {"id": "e1"}}, None)),
            mock.patch.object(Executor, "_place_exit_leg",
                              lambda self, placer, kind, **kw: ({"order": {"id": "x"}}, None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        res = ex._open(self._intent())
        self.assertTrue(res.ok, res.error)
        self.assertEqual([], cli.margin_calls)
        self.assertNotIn("margin_mode", res.detail)


if __name__ == "__main__":
    unittest.main()
