# -*- coding: utf-8 -*-
"""LLM 稳定性测试：重试 + 降级。"""
from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from omnialpha.strategist.llm_client import LLMClient, LLMConfig, LLMError


class TestLLMRetry(unittest.TestCase):
    def _client(self, **over):
        cfg = LLMConfig(
            max_retries=3, retry_backoff_sec=0.0,  # 测试不等待
            **over,
        )
        c = LLMClient(cfg)
        return c

    def test_retry_on_5xx_then_success(self):
        c = self._client()
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(req.full_url, 502, "bad gw", {}, None)
            resp = mock.MagicMock()
            resp.read.return_value = b'{"ok":1}'
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda *a: None
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            raw = c._post_with_retry(mock.MagicMock(full_url="http://x"))
        self.assertEqual(calls["n"], 3, "应重试到第 3 次成功")
        self.assertIn("ok", raw)

    def test_no_retry_on_4xx(self):
        c = self._client()
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "unauth", {}, None)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(LLMError):
                c._post_with_retry(mock.MagicMock(full_url="http://x"))
        self.assertEqual(calls["n"], 1, "4xx 不应重试")

    def test_retry_on_network_error(self):
        c = self._client()
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionResetError("10054")
            resp = mock.MagicMock()
            resp.read.return_value = b'{"ok":1}'
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda *a: None
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            raw = c._post_with_retry(mock.MagicMock(full_url="http://x"))
        self.assertEqual(calls["n"], 2)

    def test_exhausted_retries_raises(self):
        c = self._client()
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 503, "down", {}, None)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(LLMError):
                c._post_with_retry(mock.MagicMock(full_url="http://x"))
        self.assertEqual(calls["n"], 3, "应重试满 3 次")


class TestHoldFallback(unittest.TestCase):
    def test_fallback_returns_hold_ok(self):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        cfg = StrategistConfig(symbols=["BTC_USDT"])
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = cfg
        out = r._hold_fallback("c1", "manual", "llm_failed: 502")
        self.assertTrue(out["ok"], "降级应仍 ok=True（不丢票）")
        self.assertIn("degraded", out)
        self.assertEqual(out["plan"]["chips"][0]["action"], "hold")


if __name__ == "__main__":
    unittest.main()
