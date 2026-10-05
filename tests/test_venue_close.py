"""保护单用 venue-computed close（Gate 的 `size=0` + `close=true`）。

背景：每轮重挂入场单会各留一组带**显式张数**的 TP/SL，而
`_is_orphan_protector` 只按方向判定 → 保护单只增不减（实测 30 个 / 202 张
vs 4 张持仓）。Gate 原生支持 `size=0` + `close=true`（dual 模式配
`auto_size=close_long|close_short`），语义是「平掉该方向全部仓位」——
保护单**不需要跟踪张数**，从根上消除对齐问题。

**默认关闭**（`account_risk.use_venue_close`），因为需先在目标交易所实测
`price_orders` 是否接受这些字段。默认路径行为完全不变。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402


class _Client:
    def __init__(self, mode="dual"):
        self.mode = mode
        self.bodies = []

    def banner(self):
        return ""

    def get_position_mode(self):
        return self.mode

    def place_price_order(self, body):
        self.bodies.append(body)
        return {"id": "po-1", "status": "open"}

    def get_contract(self, symbol):
        class _M:
            quanto_multiplier = 0.0001
            order_size_min = 1
            price_tick = 0.1
            order_price_round = 0.1
            min_notional_usd = 1.0
        return _M()


def _intent(side="long"):
    return parse_signal({
        "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
        "price": 50000, "sl": 49000, "tp": 51000, "side": side,
    }).intents[0]


class TestVenueClose(unittest.TestCase):
    def _ex(self, client, **kw):
        return Executor(client, symbols_whitelist=["BTC_USDT"],
                        root=Path(tempfile.mkdtemp()), bot_id="b1", **kw)

    def test_default_uses_explicit_size(self):
        """默认（未配 use_venue_close）→ 显式张数，行为不变。"""
        client = _Client()
        ex = self._ex(client, account_risk={})
        ex._place_trigger(_intent(), "short", 51000.0, True, client.get_contract("BTC_USDT"), 7)
        init = client.bodies[0]["initial"]
        self.assertEqual(init["size"], -7, "默认应带显式张数")
        self.assertNotIn("close", init)
        self.assertNotIn("auto_size", init)

    def test_venue_close_sets_size_zero_and_close(self):
        client = _Client(mode="single")
        ex = self._ex(client, account_risk={"use_venue_close": True})
        ex._place_trigger(_intent(), "short", 51000.0, True, client.get_contract("BTC_USDT"), 7)
        init = client.bodies[0]["initial"]
        self.assertEqual(init["size"], 0, "venue close 应用 size=0")
        self.assertTrue(init["close"])
        self.assertNotIn("auto_size", init, "单向模式不需要 auto_size")
        self.assertTrue(init["reduce_only"], "保护单必须 reduce_only")

    def test_dual_mode_adds_auto_size(self):
        client = _Client(mode="dual")
        ex = self._ex(client, account_risk={"use_venue_close": True})
        ex._place_trigger(_intent("long"), "short", 51000.0, True,
                          client.get_contract("BTC_USDT"), 7)
        init = client.bodies[0]["initial"]
        self.assertEqual(init["auto_size"], "close_long", "平多应写 close_long")

    def test_dual_short_uses_close_short(self):
        client = _Client(mode="dual")
        ex = self._ex(client, account_risk={"use_venue_close": True})
        it = parse_signal({
            "action": "open_short", "symbol": "BTC_USDT", "size_usd": 100,
            "price": 50000, "sl": 51000, "tp": 49000,
        }).intents[0]
        ex._place_trigger(it, "long", 49000.0, True, client.get_contract("BTC_USDT"), 5)
        self.assertEqual(client.bodies[0]["initial"]["auto_size"], "close_short")

    def test_text_still_carries_tp_sl_suffix(self):
        """`text` 后缀必须保留 —— `_is_tp_sl` / 孤儿扫描都靠它识别保护单。"""
        client = _Client()
        ex = self._ex(client, account_risk={"use_venue_close": True})
        ex._place_trigger(_intent(), "short", 51000.0, True, client.get_contract("BTC_USDT"), 7)
        self.assertTrue(client.bodies[0]["initial"]["text"].endswith("-tp"))


if __name__ == "__main__":
    unittest.main()
