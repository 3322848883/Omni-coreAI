# -*- coding: utf-8 -*-
"""适配器必须暴露底层客户端的**完整接口**。

背景：`GateExchange` 曾漏掉 6 个 `GateClient` 公开方法的透传 ——
`get_available_usdt`（executor 的 `size_pct`/`margin_pct` 分支，4 处调用）、
`place_trailing_order` / `stop_trailing_orders`（`trail` / `cancel_trail_all`，
都在 schema 允许集里 → 可达）、`public_get` / `get_contracts` / `rest_signed_request`。
调用即 `AttributeError`。

**这个缺口在单测里完全隐形**：测试用的假客户端实现了完整接口，所以测试全绿。
真正暴露它的只有生产 —— 而同类问题（触发子系统的 `public_get` 缺失）已经让
功能**静默死了 178 个周期**。

所以这里用内省把不变量钉住：**GateClient 的每个公开方法，GateExchange 都要有**。
将来给 GateClient 加方法而忘了透传，这条测试会立刻失败。
"""
from __future__ import annotations

import inspect
import unittest

from omnialpha.exchanges import binance, bitget, bybit, gate, hyperliquid, okx
from omnialpha.exchanges.base import ExchangeClient
from omnialpha.exchanges.gate import GateExchange
from omnialpha.gate_client import GateClient
from omnialpha.paper.exchange import PaperExchange

ADAPTERS = [
    ("gate", gate.GateExchange),
    ("binance", binance.BinanceExchange),
    ("okx", okx.OkxExchange),
    ("bybit", bybit.BybitExchange),
    ("bitget", bitget.BitgetExchange),
    ("hyperliquid", hyperliquid.HyperliquidExchange),
    ("paper", PaperExchange),
]

# 有意不透传的方法。留空 = 要求完整透传；新增例外必须写在这里并注明原因。
EXCLUDED: set[str] = set()

# 真实生产调用点依赖的那 6 个（回归钉）
REGRESSION = (
    "get_available_usdt",
    "get_contracts",
    "place_trailing_order",
    "stop_trailing_orders",
    "public_get",
    "rest_signed_request",
)


def _public(cls) -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


class TestGateExchangeProxyCompleteness(unittest.TestCase):
    def test_proxies_every_public_gate_client_method(self):
        missing = sorted(_public(GateClient) - _public(GateExchange) - EXCLUDED)
        self.assertEqual(
            missing, [],
            "GateExchange 未透传这些 GateClient 公开方法，调用方会在运行时 "
            f"AttributeError：{missing}。请补上包装（需 symbol 映射的走 self._s()），"
            "或加进 EXCLUDED 并说明原因。",
        )

    def test_the_six_that_were_missing(self):
        """钉住这 6 个具体方法 —— 它们有真实调用点，不是理论缺口。"""
        for name in REGRESSION:
            self.assertTrue(
                hasattr(GateExchange, name), f"回归：{name} 又不透传了"
            )

    def test_excluded_list_is_honest(self):
        """EXCLUDED 里的名字必须真的存在于 GateClient，避免写错名字变成假例外。"""
        bogus = sorted(EXCLUDED - _public(GateClient))
        self.assertEqual(bogus, [], f"EXCLUDED 里有 GateClient 上不存在的名字：{bogus}")


class TestAllAdaptersImplementBase(unittest.TestCase):
    """**所有**交易所适配器都必须实现 `ExchangeClient` 的抽象方法。

    `ExchangeClient` 有 20 个 `raise NotImplementedError` 的抽象方法。此前
    **没有任何测试保证适配器实现它们** —— 漏掉一个的话，调用方只会在运行时
    撞 `NotImplementedError`（和 `GateExchange` 缺 `public_get` 那次同类）。
    这里把不变量钉住：**将来新增适配器或新增抽象方法，漏实现会立刻失败。**
    """

    def _abstract_names(self) -> set[str]:
        out = set()
        for name, fn in ExchangeClient.__dict__.items():
            if not callable(fn) or name.startswith("_"):
                continue
            try:
                src = inspect.getsource(fn)
            except (OSError, TypeError):
                continue
            if "NotImplementedError" in src:
                out.add(name)
        return out

    def test_every_adapter_overrides_every_abstract_method(self):
        abstract = self._abstract_names()
        self.assertGreaterEqual(len(abstract), 15, "抽象方法数异常，检查基类是否被改动")
        bad = {}
        for name, cls in ADAPTERS:
            missing = sorted(abstract - set(cls.__dict__))
            if missing:
                bad[name] = missing
        self.assertEqual(
            bad, {},
            f"这些适配器没有实现基类抽象方法（调用时会 NotImplementedError）：{bad}",
        )


if __name__ == "__main__":
    unittest.main()
