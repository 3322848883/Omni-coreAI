"""OpenAI-compatible chat completions client (stdlib only)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


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

    def chat(self, system: str, user: str) -> str:
        return self.chat_messages(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )

    def chat_messages(self, messages: list, tools: Optional[list] = None,
                      tool_choice: Optional[str] = None) -> str:
        return self.chat_message_full(messages, tools=tools, tool_choice=tool_choice).get("content") or ""

    def chat_message_full(self, messages: list, tools: Optional[list] = None,
                          tool_choice: Optional[str] = None) -> dict:
        """Return assistant message dict: content, reasoning_content, tool_calls."""
        url = f"{self.cfg.base_url()}/chat/completions"
        body: dict = {
            "model": self.cfg.effective_model(),
            "max_tokens": self.cfg.effective_max_tokens(),
            "messages": messages,
        }
        if self.cfg.thinking:
            # official OpenAI-compatible shape
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = (self.cfg.reasoning_effort or "high").lower()
            # temperature / presence_penalty ignored in thinking mode — do not send
        else:
            body["temperature"] = self.cfg.temperature
        if self.cfg.user_id:
            body["user_id"] = self.cfg.user_id
        if self.cfg.json_mode and not tools:
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
                "Authorization": f"Bearer {self.cfg.api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "replace")[:400]
            raise LLMError(f"LLM HTTP {e.code}: {err}") from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM request failed: {e}") from e
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            msg = choice["message"] or {}
            content = msg.get("content")
            self.last_reasoning = msg.get("reasoning_content") or ""
            self.last_finish_reason = choice.get("finish_reason")
            self.last_usage = data.get("usage") or {}
            self.last_model = data.get("model") or self.cfg.effective_model()
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
