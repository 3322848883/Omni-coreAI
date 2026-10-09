# -*- coding: utf-8 -*-
"""T16 后续 + T20：真实合约元数据 + `quanto` 收紧 + `account_scope` 熔断归属。

- Bitget / Hyperliquid 的 `get_contract` 原先对**所有币**硬编码一套值 —— 最要紧的是
  `quanto_multiplier`（合约面值），它直接决定「张数 ↔ 名义金额」的换算。
- `sizing.usd_to_contracts` 原先在 `quanto` 缺失时**静默兜底 1.0**，而 paper 路径对
  同样输入是**硬拒**的 —— 两条路径差几个数量级，live 会悄悄下一笔大单而回测里同样的
  输入直接报错（spec S2.3 #5）。
- `account_scope` 原先只存在配置层、不参与熔断归属：同一账户上的多个 bot 会各自熔断，
  一个停了另一个还在拿同一笔钱开仓（T19）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.exchanges.bitget import BitgetExchange  # noqa: E402
from omnialpha.exchanges.hyperliquid import HyperliquidExchange  # noqa: E402
from omnialpha.gate_client import ContractMeta, GateApiError  # noqa: E402
from omnialpha.sizing import usd_to_contracts  # noqa: E402


class TestQuantoMissingIsRefused(unittest.TestCase):
    def test_missing_quanto_raises(self):
        """缺失 → 硬拒（与 paper 同源），不再静默当 1.0。"""
        meta = ContractMeta(name="X_USDT", quanto_multiplier=None,
                            order_size_round=1.0, order_price_round=0.1, leverage_max=20)
        with self.assertRaises(GateApiError) as cm:
            usd_to_contracts(100.0, 50000.0, meta)
        self.assertIn("quanto_multiplier", str(cm.exception))

    def test_zero_or_negative_quanto_raises(self):
        for bad in (0.0, -1.0):
            with self.subTest(quanto=bad):
                meta = ContractMeta(name="X_USDT", quanto_multiplier=bad,
                                    order_size_round=1.0, order_price_round=0.1, leverage_max=20)
                with self.assertRaises(GateApiError):
                    usd_to_contracts(100.0, 50000.0, meta)

    def test_real_quanto_still_works(self):
        meta = ContractMeta(name="BTC_USDT", quanto_multiplier=0.0001,
                            order_size_round=1.0, order_price_round=0.1, leverage_max=20)
        # 100 USDT / (50000 × 0.0001) = 20 张
        self.assertEqual(usd_to_contracts(100.0, 50000.0, meta), 20)


class TestBitgetContractMeta(unittest.TestCase):
    def _ex(self, payload):
        ex = BitgetExchange(env="live", api_key="k", api_secret="s")
        ex._req = lambda *a, **kw: payload  # type: ignore[method-assign]
        return ex

    def test_reads_real_values(self):
        ex = self._ex({"data": [{"sizeMultiplier": "0.0001", "pricePlace": "1",
                                 "volumePlace": "0", "maxLever": "125"}]})
        m = ex.get_contract("BTC_USDT")
        self.assertAlmostEqual(m.quanto_multiplier, 0.0001)
        self.assertAlmostEqual(m.order_price_round, 0.1)
        self.assertAlmostEqual(m.order_size_round, 1.0)
        self.assertAlmostEqual(m.leverage_max, 125.0)

    def test_falls_back_when_field_missing(self):
        """取不到面值就整体退回默认（不拿猜出来的面值算张数）。"""
        ex = self._ex({"data": [{"pricePlace": "1"}]})
        m = ex.get_contract("BTC_USDT")
        self.assertEqual(m.quanto_multiplier, 1.0)
        self.assertEqual(m.leverage_max, 100)

    def test_falls_back_on_api_error(self):
        ex = BitgetExchange(env="live", api_key="k", api_secret="s")

        def boom(*a, **kw):
            raise RuntimeError("network down")

        ex._req = boom  # type: ignore[method-assign]
        m = ex.get_contract("BTC_USDT")
        self.assertEqual(m.quanto_multiplier, 1.0)


class TestHyperliquidContractMeta(unittest.TestCase):
    def _ex(self, payload):
        ex = HyperliquidExchange(env="live", api_key="k", api_secret="s")
        ex._info = lambda *a, **kw: payload  # type: ignore[method-assign]
        return ex

    def test_reads_sz_decimals_and_leverage(self):
        ex = self._ex({"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                                   {"name": "DOGE", "szDecimals": 0, "maxLeverage": 10}]})
        btc = ex.get_contract("BTC_USDT")
        self.assertEqual(btc.quanto_multiplier, 1.0, "HL 的数量单位就是币本身")
        self.assertAlmostEqual(btc.order_size_round, 1e-5)
        self.assertAlmostEqual(btc.order_price_round, 0.1)   # 6 - 5 = 1 位小数
        self.assertAlmostEqual(btc.leverage_max, 40.0)

        doge = ex.get_contract("DOGE_USDT")
        self.assertAlmostEqual(doge.order_size_round, 1.0)
        self.assertAlmostEqual(doge.order_price_round, 1e-6)  # 6 - 0 = 6 位小数
        self.assertAlmostEqual(doge.leverage_max, 10.0)

    def test_unknown_coin_falls_back(self):
        ex = self._ex({"universe": [{"name": "BTC", "szDecimals": 5}]})
        m = ex.get_contract("PEPE_USDT")
        self.assertAlmostEqual(m.order_size_round, 0.001)
        self.assertEqual(m.leverage_max, 50)


class TestAccountScopeIsAuthoritative(unittest.TestCase):
    """`account_scope` 决定熔断归属（同账户的 bot 必须一起停）。"""

    def _yaml(self, td: Path, body: str) -> Path:
        d = td / "config" / "bots"
        d.mkdir(parents=True)
        p = d / "b1.yaml"
        p.write_text(body, encoding="utf-8")
        return p

    BASE = ("bot_id: b1\nenabled: false\nsymbols: [BTC_USDT]\n"
            "label_prefix: b1\n")

    def test_bot_scope_forces_bot_local_halt(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._yaml(Path(td), self.BASE + "account: acct-a\naccount_scope: bot\n")
            cfg = load_bot_config(p)
            self.assertEqual(cfg.account, "", "`bot` = 显式声明独立，即使配了 account")
            self.assertEqual(cfg.account_scope, "bot")

    def test_named_scope_wins_over_account_field(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._yaml(Path(td), self.BASE + "account: acct-a\naccount_scope: acct-b\n")
            cfg = load_bot_config(p)
            self.assertEqual(cfg.account, "acct-b")

    def test_auto_keeps_legacy_account(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._yaml(Path(td), self.BASE + "account: acct-a\n")
            cfg = load_bot_config(p)
            self.assertEqual(cfg.account, "acct-a", "auto 沿用旧字段（行为不变）")


if __name__ == "__main__":
    unittest.main()
