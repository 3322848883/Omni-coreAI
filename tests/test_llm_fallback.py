"""备用供应商链：主模型不可用时切备用，而不是让整轮决策降级 hold。

背景（2026-10-05 实测）：网关 `gw-flash` 返回
  429 `usage exceeds frequency limit, but don't worry ... alternatively, you can
       switch to the other models to continue using`
  503 `no_healthy_account`（`no healthy account available in pool`）
而这两条路原先**都只通向 `_hold_fallback`（降级 hold）** —— 等于这一轮决策直接
作废。`ethdisc-persona.err` 里 17:46 那批就是这个形态。

修法：`providers.yaml` 的供应商条目可写 `fallback`（单个名字或列表），
bot 也可用 `llm.fallback` 覆盖；`resolve_llm_config` 把它展开成 `cfg.fallbacks`，
`LLMClient.chat_message_full` 按「主 → 备用 1 → 备用 2」顺序试。
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.providers import (  # noqa: E402
    MAX_FALLBACKS,
    ProviderError,
    resolve_llm_config,
)
from omnialpha.strategist.llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402

_KEY_ENV = "OMNIALPHA_TEST_LLM_KEY"

# 每个 provider 各留一个 {pN_extra} 缩进槽位 —— 往 YAML 末尾追加会挂到最后一个
# provider 上（实测踩过），所以必须插在指定位置。
_CFG_YAML = """
default: p1
providers:
  p1:
    base_url: http://gw:1/v1
    api_key_env: {key_env}
    model: model-a
    thinking: true
    reasoning_effort: max
    timeout_sec: 30
    max_tokens: 1000
{p1_extra}
  p2:
    base_url: http://gw:2/v1
    api_key_env: {key_env}
    model: model-b
    thinking: false
    json_mode: false
    timeout_sec: 45
{p2_extra}
  p3:
    base_url: http://gw:3/v1
    api_key_env: {key_env}
    model: model-c
"""


def _root(td, p1_extra="", p2_extra=""):
    (td / "config").mkdir(parents=True, exist_ok=True)
    (td / "config" / "providers.yaml").write_text(
        _CFG_YAML.format(key_env=_KEY_ENV, p1_extra=p1_extra, p2_extra=p2_extra),
        encoding="utf-8")
    return td


class _Resp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeHTTP:
    """按请求体里的 model 决定成功还是失败，并记录调用顺序与 timeout。"""

    def __init__(self, fail_models=(), status=429):
        self.fail_models = set(fail_models)
        self.status = status
        self.seen: list[tuple[str, float]] = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        model = str(body.get("model") or "")
        self.seen.append((model, timeout))
        if model in self.fail_models:
            raise urllib.error.HTTPError(
                req.full_url, self.status, "boom", {},
                io.BytesIO(json.dumps({"error": {"code": "rate_limit_exceeded"}}).encode()))
        return _Resp({
            "choices": [{"message": {"content": f"from {model}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3},
            "model": model,
        })


class TestResolveChain(unittest.TestCase):
    def setUp(self):
        os.environ[_KEY_ENV] = "k"

    def test_no_fallback_is_empty(self):
        """没配 fallback → 空链，行为与升级前一致。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(_root(Path(t)), bot_id="b", llm={})
            self.assertEqual(cfg.fallbacks, [])
            self.assertEqual(cfg.name, "p1")

    def test_provider_level_single_name(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(_root(Path(t), p1_extra="    fallback: p2"),
                                     bot_id="b", llm={})
            self.assertEqual([c.model for c in cfg.fallbacks], ["model-b"])
            self.assertEqual(cfg.fallbacks[0].name, "p2")

    def test_provider_level_list_keeps_order(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(_root(Path(t), p1_extra="    fallback: [p2, p3]"),
                                     bot_id="b", llm={})
            self.assertEqual([c.model for c in cfg.fallbacks], ["model-b", "model-c"])

    def test_bot_level_overrides_provider_level(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(_root(Path(t), p1_extra="    fallback: p2"),
                                     bot_id="b", llm={"fallback": "p3"})
            self.assertEqual([c.model for c in cfg.fallbacks], ["model-c"])

    def test_fallback_does_not_inherit_bot_model_override(self):
        """bot 的 `model` 覆盖只针对主供应商 —— 套到备用上会把它改成同一个模型。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(_root(Path(t), p1_extra="    fallback: p2"),
                                     bot_id="b", llm={"model": "model-x"})
            self.assertEqual(cfg.model, "model-x")
            self.assertEqual([c.model for c in cfg.fallbacks], ["model-b"])

    def test_chain_is_recursive(self):
        """p1 → p2 → p3（备用自己的 fallback 也要展开）。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(
                _root(Path(t), p1_extra="    fallback: p2", p2_extra="    fallback: p3"),
                bot_id="b", llm={})
            self.assertEqual([c.model for c in cfg.fallbacks], ["model-b", "model-c"])

    def test_cycle_does_not_loop_forever(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(
                _root(Path(t), p1_extra="    fallback: p2", p2_extra="    fallback: p1"),
                bot_id="b", llm={})
            self.assertEqual([c.model for c in cfg.fallbacks], ["model-b"])

    def test_chain_length_is_capped(self):
        """链长上限：每个备用都要先耗完前一个的重试退避，不能无限长。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(
                _root(Path(t), p1_extra="    fallback: [p2, p3]",
                      p2_extra="    fallback: p3"),
                bot_id="b", llm={})
            self.assertLessEqual(len(cfg.fallbacks), MAX_FALLBACKS)
            self.assertEqual([c.model for c in cfg.fallbacks], ["model-b", "model-c"])

    def test_unknown_fallback_raises(self):
        """指向不存在的供应商必须报错 —— 静默无备用正是「静默失效」形态。"""
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ProviderError):
                resolve_llm_config(_root(Path(t), p1_extra="    fallback: nope"),
                                   bot_id="b", llm={})

    def test_bad_type_raises(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ProviderError):
                resolve_llm_config(_root(Path(t)), bot_id="b",
                                   llm={"fallback": {"name": "p2"}})

    def test_fallback_keeps_own_transport_settings(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = resolve_llm_config(_root(Path(t), p1_extra="    fallback: p2"),
                                     bot_id="b", llm={})
            fb = cfg.fallbacks[0]
            self.assertEqual(fb.default_base_url, "http://gw:2/v1")
            self.assertEqual(fb.timeout_sec, 45)
            self.assertFalse(fb.thinking)


class TestClientChain(unittest.TestCase):
    def setUp(self):
        os.environ[_KEY_ENV] = "k"

    def _client(self, fallbacks=()):
        cfg = LLMConfig(name="primary", model="model-a",
                        default_base_url="http://gw:1/v1", api_key_env=_KEY_ENV,
                        max_retries=1, retry_backoff_sec=0.0,
                        fallbacks=list(fallbacks))
        return LLMClient(cfg)

    def _fb(self, name="backup", model="model-b", timeout=45):
        return LLMConfig(name=name, model=model, default_base_url="http://gw:2/v1",
                         api_key_env=_KEY_ENV, max_retries=1,
                         retry_backoff_sec=0.0, timeout_sec=timeout)

    def test_primary_ok_does_not_touch_fallback(self):
        http = _FakeHTTP()
        c = self._client([self._fb()])
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", http):
            out = c.chat_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "from model-a")
        self.assertEqual([m for m, _ in http.seen], ["model-a"])
        self.assertEqual(c.last_fallback_index, 0)
        self.assertEqual(c.last_provider, "primary")

    def test_falls_back_on_429(self):
        http = _FakeHTTP(fail_models=["model-a"])
        c = self._client([self._fb()])
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", http):
            out = c.chat_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "from model-b")
        self.assertEqual([m for m, _ in http.seen], ["model-a", "model-b"])
        self.assertEqual(c.last_fallback_index, 1)
        self.assertEqual(c.last_provider, "backup")

    def test_falls_back_on_4xx_auth_error(self):
        """鉴权/参数错（4xx 非 429）不重试，但**仍然切备用** —— 换个供应商可能就行。"""
        http = _FakeHTTP(fail_models=["model-a"], status=401)
        c = self._client([self._fb()])
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", http):
            out = c.chat_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "from model-b")

    def test_second_fallback_used_when_first_also_fails(self):
        http = _FakeHTTP(fail_models=["model-a", "model-b"])
        c = self._client([self._fb(), self._fb(name="third", model="model-c")])
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", http):
            out = c.chat_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "from model-c")
        self.assertEqual([m for m, _ in http.seen], ["model-a", "model-b", "model-c"])
        self.assertEqual(c.last_fallback_index, 2)

    def test_all_failed_raises(self):
        http = _FakeHTTP(fail_models=["model-a", "model-b"])
        c = self._client([self._fb()])
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", http):
            with self.assertRaises(LLMError):
                c.chat_messages([{"role": "user", "content": "hi"}])
        self.assertEqual([m for m, _ in http.seen], ["model-a", "model-b"])

    def test_no_fallback_behaves_as_before(self):
        http = _FakeHTTP(fail_models=["model-a"])
        c = self._client()
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", http):
            with self.assertRaises(LLMError):
                c.chat_messages([{"role": "user", "content": "hi"}])
        self.assertEqual([m for m, _ in http.seen], ["model-a"])

    def test_fallback_uses_its_own_timeout(self):
        """重试策略/超时必须取自**当前正在试的**供应商，否则备用配置被静默覆盖。"""
        http = _FakeHTTP(fail_models=["model-a"])
        c = self._client([self._fb(timeout=45)])
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", http):
            c.chat_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(http.seen[-1][1], 45)

    def test_empty_content_falls_back(self):
        """主供应商回空内容（finish_reason=length 之类）也算失败 → 切备用。"""
        calls = {"n": 0}

        def _urlopen(req, timeout=None):
            body = json.loads(req.data.decode("utf-8"))
            calls["n"] += 1
            if body.get("model") == "model-a":
                return _Resp({"choices": [{"message": {"content": ""},
                                           "finish_reason": "length"}], "model": "model-a"})
            return _Resp({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                          "model": body.get("model")})

        c = self._client([self._fb()])
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", _urlopen):
            self.assertEqual(c.chat_messages([{"role": "user", "content": "hi"}]), "ok")
        self.assertEqual(calls["n"], 2)

    def test_usage_accumulates_across_fallback(self):
        """usage 累计必须跨供应商累加（成本统计不能漏掉备用那一段）。"""
        http = _FakeHTTP(fail_models=["model-a"])
        c = self._client([self._fb()])
        with mock.patch("omnialpha.strategist.llm_client.urllib.request.urlopen", http):
            c.chat_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(c.prompt_tokens(), 3)


if __name__ == "__main__":
    unittest.main()
