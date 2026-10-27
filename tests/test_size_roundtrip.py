# -*- coding: utf-8 -*-
"""四种仓位口径的**往返一致性**：张 ↔ 币 ↔ 名义U ↔ 保证金。

要求（用户）：各种形式相互转换不应该有误差。

定义「无误差」：
  · 输入必须是**步长整数倍**（合法的下单量）；
  · 四种口径换算回张数，必须**逐位相等**（Decimal 比较，不是 float 近似）。

覆盖 5 个代表币种，每个 1..40 个步长 × 4 种口径 = 800 组/币种。
若这里红了，说明换算链引入了 float 噪声或提前 floor 掉了合法量。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402

PRICES = {
    "ETH_USDT": 2340.5,
    "SOL_USDT": 168.42,
    "BTC_USDT": 61234.5,
    "XRP_USDT": 2.3417,
    "PEPE_USDT": 0.0000067,
}

# (symbol, quanto, step)
COINS = (
    ("ETH_USDT", 0.01, 0.1),
    ("SOL_USDT", 1.0, 0.1),
    ("BTC_USDT", 0.0001, 1.0),
    ("XRP_USDT", 10.0, 0.1),
    ("PEPE_USDT", 1e7, 0.1),
)

LEV = 50


class _Client:
    def get_last_price(self, symbol):
        return PRICES[symbol]

    def get_available_usdt(self):
        return 10000.0


def _meta(sym, quanto, step) -> ContractMeta:
    return ContractMeta(name=sym, quanto_multiplier=quanto, order_size_round=1,
                        order_price_round=0.01, leverage_max=LEV,
                        size_min=0 if step < 1 else 1, size_max=1e9,
                        enable_decimal=step < 1)


def _exec() -> Executor:
    import tempfile
    return Executor(_Client(), root=Path(tempfile.mkdtemp()), label_prefix="rt", bot_id="rt")


def _contracts(payload: dict, m: ContractMeta) -> D:
    intent = parse_signal(payload).intents[0]
    q, _, _ = _exec()._resolve_order_size(intent, m)
    return D(str(q))


class TestRoundTrip(unittest.TestCase):
    def test_all_forms_roundtrip_exactly(self):
        total = 0
        for sym, quanto, step in COINS:
            m = _meta(sym, quanto, step)
            q0, st = D(str(quanto)), D(str(step))
            px = D(str(PRICES[sym]))
            for k in range(1, 41):
                q = st * k                       # 合法张数（步长整数倍）
                coins = q * q0
                usd = q * px * q0
                margin = usd / D(LEV)
                forms = {
                    "size": {"size": float(q)},
                    "size_coins": {"size_coins": float(coins)},
                    "size_usd": {"size_usd": float(usd)},
                    "margin_usd": {"margin_usd": float(margin), "leverage": LEV},
                }
                for name, extra in forms.items():
                    got = _contracts({"action": "open_long", "symbol": sym, **extra}, m)
                    self.assertEqual(
                        got, q,
                        f"{sym} {name}: 期望 {q} 张，得 {got} 张"
                        f"（输入 {extra}，币 {coins}，名义 {usd}）")
                    total += 1
        self.assertEqual(total, len(COINS) * 40 * 4)

    def test_roundtrip_is_idempotent(self):
        """连续换算两次结果不变（无漂移）。"""
        for sym, quanto, step in COINS:
            m = _meta(sym, quanto, step)
            q = D(str(step)) * 7
            coins = q * D(str(quanto))
            once = _contracts({"action": "open_long", "symbol": sym,
                               "size_coins": float(coins)}, m)
            twice = _contracts({"action": "open_long", "symbol": sym,
                                "size_coins": float(once * D(str(quanto)))}, m)
            self.assertEqual(once, twice, f"{sym}: {once} != {twice}")

    def test_offgrid_floors_to_nearest_lower_and_stays_stable(self):
        """非整倍数输入：往下取到最近一档，且再算一次不变（不抖动）。"""
        for sym, quanto, step in COINS:
            m = _meta(sym, quanto, step)
            q0, st = D(str(quanto)), D(str(step))
            q_expect = st * 13
            coins_offgrid = (q_expect + st / D(2)) * q0     # 差了半个步长
            got = _contracts({"action": "open_long", "symbol": sym,
                              "size_coins": float(coins_offgrid)}, m)
            self.assertEqual(got, q_expect, f"{sym}: 半档应 floor 到 {q_expect}，得 {got}")
            # 用 floor 后的量再走一轮，必须稳定
            got2 = _contracts({"action": "open_long", "symbol": sym,
                               "size_coins": float(got * q0)}, m)
            self.assertEqual(got2, got, f"{sym}: 二次换算漂移 {got} → {got2}")


if __name__ == "__main__":
    unittest.main()
