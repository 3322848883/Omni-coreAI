"""OpenAI-compatible chat completions client (stdlib only)."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


@dataclass
class LLMConfig:
    base_url_env: str = "OPENAI_BASE_URL"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "deepseek-flash"  # official: deepseek-flash | deepseek-v4-pro
    temperature: float = 0.2
    timeout_sec: int = 120
    max_tokens: int = 8192
    default_base_url: str = "https://api.deepseek.com/v1"
    # DeepSeek thinking / reasoning (official docs)
    thinking: bool = True
    # none | low | high | max  (default max per product request)
    reasoning_effort: str = "max"
    # optional KV-cache / rate-limit isolation
    user_id: str = ""
    # official JSON Output: guarantee valid JSON (prompt must contain "json" + sample)
    json_mode: bool = True
    # 稳定性：5xx/网络错误重试（4xx 不重试）
    max_retries: int = 3
    retry_backoff_sec: float = 1.5
    # 备用供应商链：主供应商重试耗尽后按顺序切。空 = 无备用（行为与升级前一致）。
    # 单点模型不可用不该让整个 bot 停摆 —— 实测网关会返回 429
    # `usage exceeds frequency limit ... alternatively, you can switch to the other models`
    # 与 503 `no_healthy_account`，而这两条路原先都只通向「降级 hold」。
    fallbacks: list = field(default_factory=list)
    # 供应商名（providers.yaml 的键）。只用于日志与审计，不参与请求体。
    name: str = ""

    def base_url(self) -> str:
        return (
            os.environ.get(self.base_url_env)
            or os.environ.get("OPENAI_BASE_URL")
            or self.default_base_url
        ).rstrip("/")

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env) or os.environ.get("OPENAI_API_KEY") or ""
        if not key:
            raise LLMError(f"missing LLM api key env: {self.api_key_env}")
        return key

    def effective_model(self) -> str:
        m = (self.model or "").strip() or "deepseek-flash"
        return m

    def effective_max_tokens(self) -> int:
        n = int(self.max_tokens or 0)
        if self.thinking:
            # official: thinking default 64K; max effort up to 128K
            want = 65536 if (self.reasoning_effort or "").lower() != "max" else 128000
            return max(n, want) if n < want else n
        return n or 8192


class LLMClient:
    def __init__(self, cfg: Optional[LLMConfig] = None):
        self.cfg = cfg or LLMConfig()
        # 本轮累计 usage（工具循环会调多次 LLM，单次 last_usage 会漏算）
        self.usage_total: dict = {}

    def reset_usage(self) -> None:
        """清零本轮 usage 累计。每轮 Plan 开始时调一次。"""
        self.usage_total = {}

    @staticmethod
    def _sum_usage(acc: dict, usage: dict) -> dict:
        """把一次调用的 usage 累加进 acc（嵌套 dict 递归；非数值跳过）。"""
        for k, v in (usage or {}).items():
            if isinstance(v, dict):
                acc[k] = LLMClient._sum_usage(dict(acc.get(k) or {}), v)
            elif isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            else:
                acc[k] = acc.get(k, 0) + v
        return acc

    def cache_hit_tokens(self) -> int:
        """本轮累计的 prompt 缓存命中 token 数。

        DeepSeek 用 `prompt_cache_hit_tokens`，OpenAI 形状用
        `prompt_tokens_details.cached_tokens` —— 两种都认，取不到算 0。
        """
        u = self.usage_total or {}
        hit = u.get("prompt_cache_hit_tokens")
        if hit is None:
            hit = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        try:
            return int(hit or 0)
        except (TypeError, ValueError):
            return 0

    def prompt_tokens(self) -> int:
        """本轮累计的 prompt token 数（缓存命中率的分母）。"""
        try:
            return int((self.usage_total or {}).get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            return 0

    def chat(self, system: str, user: str) -> str:
        return self.chat_messages(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )

    def _post_with_retry(self, req, cfg: Optional[LLMConfig] = None) -> str:
        """HTTP 请求 + 指数退避重试（仅重试可恢复错误：5xx / 网络中断 / 超时）。

        4xx（鉴权、参数错）不重试——重试也没用，直接抛。

        `cfg` 显式传入是为了支持备用供应商：重试策略（次数/退避/超时）必须取自
        **当前正在试的那个供应商**，而不是永远取自主供应商 —— 否则备用的超时设置
        会被主供应商的值悄悄覆盖。
        """
        cfg = cfg or self.cfg
        attempts = max(1, int(getattr(cfg, "max_retries", 3) or 3))
        base = float(getattr(cfg, "retry_backoff_sec", 1.5) or 1.5)
        last_err: Optional[Exception] = None
        for i in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=cfg.timeout_sec) as resp:
                    return resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", "replace")[:400]
                code = getattr(e, "code", 0) or 0
                # 4xx（非 429）不重试
                if 400 <= code < 500 and code != 429:
                    raise LLMError(f"LLM HTTP {code}: {err}") from e
                last_err = LLMError(f"LLM HTTP {code}: {err}")
                log.warning("LLM HTTP %s (attempt %d/%d), retrying...", code, i + 1, attempts)
            except Exception as e:  # noqa: BLE001 — 网络中断/超时/连接重置
                last_err = LLMError(f"LLM request failed: {e}")
                log.warning("LLM request error (attempt %d/%d): %s", i + 1, attempts, e)
            if i < attempts - 1:
                time.sleep(base * (2 ** i))  # 1.5s, 3s, 6s...
        raise last_err or LLMError("LLM request failed: unknown")

    def chat_messages(self, messages: list, tools: Optional[list] = None,
                      tool_choice: Optional[str] = None) -> str:
        return self.chat_message_full(messages, tools=tools, tool_choice=tool_choice).get("content") or ""

    def chat_message_full(self, messages: list, tools: Optional[list] = None,
                          tool_choice: Optional[str] = None) -> dict:
        """Return assistant message dict: content, reasoning_content, tool_calls.

        **主供应商失败会按 `cfg.fallbacks` 顺序切备用。** 这是「一个模型不可用
        就整个停摆」的解药：原先唯一的出路是 `_hold_fallback`（降级 hold），
        等于这一轮决策直接作废。实测网关会给出
        `429 rate_limit_exceeded`（并明确提示换模型）与 `503 no_healthy_account`。

        每次调用都**从主供应商开始试**（不做粘性记忆）：省下的那点重试耗时
        换不来正确性 —— 主供应商可能只是短暂抖动，粘住备用会长期用错模型。
        """
        chain = [self.cfg] + [c for c in (getattr(self.cfg, "fallbacks", None) or []) if c]
        last_err: Optional[Exception] = None
        for i, cfg in enumerate(chain):
            try:
                out = self._chat_once(cfg, messages, tools=tools, tool_choice=tool_choice)
            except LLMError as e:
                last_err = e
                if i < len(chain) - 1:
                    log.warning(
                        "LLM 供应商 %s 失败（%s）→ 切备用 #%d %s",
                        cfg.name or cfg.effective_model(), e, i + 1,
                        chain[i + 1].name or chain[i + 1].effective_model(),
                    )
                continue
            self.last_provider = cfg.name or cfg.effective_model()
            self.last_fallback_index = i
            if i:
                log.warning("LLM 已用备用供应商 #%d %s", i, self.last_provider)
            return out
        raise last_err or LLMError("LLM request failed: unknown")

    def _chat_once(self, cfg: LLMConfig, messages: list, tools: Optional[list] = None,
                   tool_choice: Optional[str] = None) -> dict:
        """对**单个**供应商发一次请求（含其自身的重试）。"""
        url = f"{cfg.base_url()}/chat/completions"
        body: dict = {
            "model": cfg.effective_model(),
            "max_tokens": cfg.effective_max_tokens(),
            "messages": messages,
        }
        if cfg.thinking:
            # official OpenAI-compatible shape
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = (cfg.reasoning_effort or "high").lower()
            # temperature / presence_penalty ignored in thinking mode — do not send
        else:
            body["temperature"] = cfg.temperature
        if cfg.user_id:
            body["user_id"] = cfg.user_id
        if cfg.json_mode and not tools:
            # JSON Output; official: prompt must contain "json" + sample (system contract)
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {cfg.api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        t0 = time.time()
        raw = self._post_with_retry(req, cfg)
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            msg = choice["message"] or {}
            content = msg.get("content")
            self.last_reasoning = msg.get("reasoning_content") or ""
            self.last_reasoning_chain = list(getattr(self, "last_reasoning_chain", []) or [])
            if self.last_reasoning:
                self.last_reasoning_chain.append(self.last_reasoning)
            self.last_finish_reason = choice.get("finish_reason")
            self.last_usage = data.get("usage") or {}
            self.usage_total = self._sum_usage(
                dict(getattr(self, "usage_total", None) or {}), self.last_usage)
            self.last_model = data.get("model") or cfg.effective_model()
            tool_calls = msg.get("tool_calls") or []
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM response parse failed: {e}") from e
        self.last_latency = time.time() - t0
        out = {
            "role": "assistant",
            "content": content or "",
            "reasoning_content": self.last_reasoning or "",
            "tool_calls": tool_calls or [],
        }
        if tool_calls:
            return out
        if not out["content"] and self.last_reasoning:
            out["content"] = self.last_reasoning
        if not out["content"]:
            raise LLMError(
                f"LLM empty content (finish_reason={getattr(self, 'last_finish_reason', None)})"
            )
        return out
