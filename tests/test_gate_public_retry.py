# -*- coding: utf-8 -*-
"""`public_get` 有界重试 + `/contracts` 过期回退的回归测试。

背景：本地直连交易所 API 的链路会抖，实测底层错误**全是 TLS 层**：
`The read operation timed out`(38) / `SSL: UNEXPECTED_EOF_WHILE_READING`(29) /
`_ssl.c:1063: The handshake operation timed out`(27) / `EOF occurred in violation of
protocol`(19) / `IncompleteRead(1168689 bytes read, 137723 more expected)`。

而 `public_get` 原来**一次失败就抛**、`get_contracts` **过期后不用已有缓存兜底** ——
于是每次网络抖动都变成一笔操作失败（模拟盘 20% 的失败源于此，其中 93% 集中在
`/contracts`：它返回约 1.1 MB，在抖动链路上极易截断）。
"""
import sys
import unittest
from unittest import mock

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import GateApiError, GateClient  # noqa: E402


def make_client() -> GateClient:
    return GateClient(api_key="k", api_secret="s", env="testnet")


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestPublicGetRetry(unittest.TestCase):
    def test_retries_transient_then_succeeds(self):
        c = make_client()
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("The read operation timed out")
            return _Resp(b'{"ok": 1}')

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            out = c.public_get("/x")
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(calls["n"], 2, "瞬时故障应重试一次后成功")

    def test_no_retry_for_non_transient(self):
        c = make_client()
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise OSError("HTTP Error 404: Not Found")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(GateApiError):
                c.public_get("/x")
        self.assertEqual(calls["n"], 1, "非瞬时错误不该重试")

    def test_gives_up_after_attempts(self):
        c = make_client()
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise OSError("SSL: UNEXPECTED_EOF_WHILE_READING")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(GateApiError):
                c.public_get("/x", attempts=2)
        self.assertEqual(calls["n"], 2, "重试次数应有界")

    def test_transient_classifier(self):
        self.assertTrue(GateClient._is_transient(OSError("The read operation timed out")))
        self.assertTrue(GateClient._is_transient(OSError("SSL: UNEXPECTED_EOF_WHILE_READING")))
        self.assertTrue(GateClient._is_transient(OSError("IncompleteRead(1 bytes read)")))
        self.assertFalse(GateClient._is_transient(OSError("HTTP Error 404: Not Found")))


class TestContractsStaleFallback(unittest.TestCase):
    def test_falls_back_to_stale_cache_on_failure(self):
        """合约元数据几乎不变：拉取失败时回退到上一份缓存，而不是让操作失败。"""
        c = make_client()
        stale = {"BTC_USDT": "meta"}
        c._contract_cache = stale
        c._contract_cache_ts = 0.0                     # 已过期 → 会尝试刷新

        def boom(*a, **k):
            raise GateApiError("public get failed /contracts: timed out")

        c.public_get = boom
        self.assertIs(c.get_contracts(max_age_sec=1), stale)

    def test_raises_when_no_cache(self):
        """首次调用（无缓存）时仍要抛 —— 不掩盖真正的不可用。"""
        c = make_client()

        def boom(*a, **k):
            raise GateApiError("public get failed /contracts: timed out")

        c.public_get = boom
        with self.assertRaises(GateApiError):
            c.get_contracts()

    def test_fresh_cache_not_refetched(self):
        c = make_client()
        c._contract_cache = {"BTC_USDT": "meta"}
        c._contract_cache_ts = __import__("time").time()

        def boom(*a, **k):
            raise AssertionError("不应重新拉取")

        c.public_get = boom
        self.assertEqual(c.get_contracts(), {"BTC_USDT": "meta"})


if __name__ == "__main__":
    unittest.main()
