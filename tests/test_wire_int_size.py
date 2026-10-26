# -*- coding: utf-8 -*-
"""Gate 下单 wire 层：`size` 必须是整型字面量（2026-10-27 实盘事故回归）。

事故链（martin-grid live，真实资金）：
- 12:34 UTC 旧代码（size 为 int）挂 TP 成功；
- 4247ef8 把下单链 size 改成 float → wire 上 `"size": -8.0`；
- 12:49 重启加载新代码 → 13:23 起 **18 轮计划全灭**，每轮
  `400 /api/v4/futures/usdt/price_orders: AUTO_INVALID_REQUEST_BODY`；
- 零副作用探针定性：同 body 用整型 `"size": 0` → 业务 label
  `AUTO_INITIAL_SIZE_OVER_RANGE`（说明 body 解析成功）；用浮点 `"size": 0.0`
  → `AUTO_INVALID_REQUEST_BODY`（body 在反序列化阶段就失败）。
  Gate 后端 Go，`initial.size` 是 int64，带小数点的数字直接解析失败。

修复：`rest_signed_request` 在 **签名与发送之前** 归一化请求体 —— 整数值 float
转 int（`-8.0 → -8`），真小数保留（0.7 张是 enable_decimal 合约的合法张数）。
"""
import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.gate_client import GateClient, _wire_body  # noqa: E402


class _Captured:
    """截获 urllib 请求，记录实际发送的 body 字节与头。"""

    def __init__(self):
        self.req = None

    def __call__(self, req, timeout=None):
        self.req = req
        out = self

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        return _Resp()


def _post(body, path="/api/v4/futures/usdt/price_orders"):
    cap = _Captured()
    client = GateClient(api_key="k", api_secret="s", env="live")
    with mock.patch("urllib.request.urlopen", cap):
        client.rest_signed_request("POST", path, "", body)
    return cap.req


class TestWireIntSize(unittest.TestCase):
    def test_integral_float_size_is_int_literal(self):
        """★核心回归★ -8.0 必须写成 -8 —— 否则 Gate 400 AUTO_INVALID_REQUEST_BODY。"""
        req = _post({"initial": {"contract": "ETH_USDT", "size": -8.0,
                                 "price": "0", "tif": "ioc", "reduce_only": True,
                                 "text": "t-mtg-tp"},
                     "trigger": {"strategy_type": 0, "price_type": 1,
                                 "price": "2364.5", "rule": 1}})
        raw = req.data.decode()
        self.assertIn('"size":-8,', raw)
        self.assertNotIn("-8.0", raw)

    def test_fractional_size_kept(self):
        """真小数张（0.7）必须保留 —— enable_decimal 合约的合法张数。"""
        req = _post({"size": -0.7, "contract": "ETH_USDT"})
        self.assertIn('"size":-0.7', req.data.decode())

    def test_signature_uses_normalized_body(self):
        """签名 body_hash 必须与实发 body 同源 —— 否则 Gate 报 INVALID_SIGNATURE。"""
        body = {"initial": {"size": -8.0, "contract": "ETH_USDT", "price": "0"}}
        req = _post(body, path="/api/v4/futures/usdt/orders")
        sent = req.data.decode()
        expect_hash = hashlib.sha512(sent.encode()).hexdigest()
        ts = req.headers["Timestamp"]
        sign_str = f"POST\n/api/v4/futures/usdt/orders\n\n{expect_hash}\n{ts}"
        expect = hmac.new(b"s", sign_str.encode(), hashlib.sha512).hexdigest()
        self.assertEqual(req.headers["Sign"], expect)
        self.assertIn('"size":-8', sent)

    def test_int_bool_str_untouched(self):
        req = _post({"size": -8, "reduce_only": True, "text": "t-mtg-tp",
                     "price": "0", "trigger": {"rule": 1, "price": "2379.5"}})
        d = json.loads(req.data.decode())
        self.assertIsInstance(d["size"], int)
        self.assertIs(d["reduce_only"], True)
        self.assertEqual(d["text"], "t-mtg-tp")
        self.assertEqual(d["trigger"]["price"], "2379.5")

    def test_callers_dict_not_mutated(self):
        """归一化返回新对象，不得改调用方的 body（重试逻辑还要读原值）。"""
        body = {"initial": {"size": -8.0}}
        _post(body)
        self.assertIsInstance(body["initial"]["size"], float)


class TestPriceOrderIntSize(unittest.TestCase):
    """`/price_orders`（auto orders）的 `initial.size` 是 int64 —— **不接受小数**。

    实测 2026-10-27（pa-eth-v5 实盘）：入场 `size_usd` 换算出 21.4 张，
    `/orders` 接受并 floor 成 21（Gate 回报 size=-21），但同一个 21.4 去挂
    TP/SL 触发单 → `AUTO_INVALID_REQUEST_BODY`；小数 0.7 同样 400。
    → 触发单必须按**整数张**下（与交易所 floor 后的真实持仓一致：21.4 → 21）。
    """

    def _place(self, body):
        cap = _Captured()
        client = GateClient(api_key="k", api_secret="s", env="live")
        with mock.patch("urllib.request.urlopen", cap):
            client.place_price_order(body)
        return cap.req

    def test_fractional_truncated_to_whole_contracts(self):
        req = self._place({"initial": {"contract": "ETH_USDT", "size": -21.4,
                                       "price": "0", "tif": "ioc",
                                       "reduce_only": True, "text": "t-pev5-tp"},
                           "trigger": {"strategy_type": 0, "price_type": 1,
                                       "price": "2310.5", "rule": 2}})
        d = json.loads(req.data.decode())
        self.assertEqual(d["initial"]["size"], -21)
        self.assertIsInstance(d["initial"]["size"], int)

    def test_positive_fractional_truncated(self):
        req = self._place({"initial": {"contract": "ETH_USDT", "size": 21.4,
                                       "price": "0", "tif": "ioc",
                                       "reduce_only": True, "text": "t-pev5-sl"},
                           "trigger": {"strategy_type": 0, "price_type": 1,
                                       "price": "2332.3", "rule": 1}})
        self.assertEqual(json.loads(req.data.decode())["initial"]["size"], 21)

    def test_below_one_contract_refused(self):
        """0.7 张 → 0 张：挂 0 张保护单 = 裸仓，必须拒（让调用方回滚入场）。"""
        from omnialpha.gate_client import GateApiError
        with self.assertRaises(GateApiError):
            self._place({"initial": {"contract": "ETH_USDT", "size": -0.7,
                                     "price": "0", "tif": "ioc",
                                     "reduce_only": True, "text": "t-pev5-tp"},
                         "trigger": {"strategy_type": 0, "price_type": 1,
                                     "price": "2310.5", "rule": 2}})

    def test_venue_close_size_zero_untouched(self):
        """`close=true`（venue-computed close）的 size=0 是合法语义，不能拒。"""
        req = self._place({"initial": {"contract": "ETH_USDT", "size": 0,
                                       "price": "0", "tif": "ioc",
                                       "reduce_only": True, "close": True,
                                       "auto_size": "close_short", "text": "t-pev5-tp"},
                           "trigger": {"strategy_type": 0, "price_type": 1,
                                       "price": "2310.5", "rule": 2}})
        self.assertEqual(json.loads(req.data.decode())["initial"]["size"], 0)


class TestWireBodyHelper(unittest.TestCase):
    def test_nested_list_and_tuple(self):
        out = _wire_body({"legs": [1.0, 0.7], "t": (2.0,)})
        self.assertEqual(out["legs"], [1, 0.7])
        self.assertEqual(out["t"], [2])

    def test_none_and_strings(self):
        self.assertEqual(_wire_body({"a": None, "b": "x"}), {"a": None, "b": "x"})

    def test_bool_is_not_int_coerced(self):
        self.assertIs(_wire_body({"f": True})["f"], True)

    def test_integral_negative_zero(self):
        out = _wire_body({"size": -0.0})
        self.assertEqual(out["size"], 0)
        self.assertIsInstance(out["size"], int)


if __name__ == "__main__":
    unittest.main()
