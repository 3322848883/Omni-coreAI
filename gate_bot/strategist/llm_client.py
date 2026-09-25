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
    model: str = "deepseek-chat"
    temperature: float = 0.2
    timeout_sec: int = 60
    max_tokens: int = 4096
    default_base_url: str = "https://api.deepseek.com/v1"
    # DeepSeek thinking / reasoning mode (default ON)
    thinking: bool = True

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
        m = (self.model or "").strip()
        if not self.thinking:
            return m or "deepseek-chat"
        # prefer explicit reasoner model when thinking is on
        if "reasoner" in m.lower() or "thinking" in m.lower():
            return m
        if m in ("deepseek-chat", "deepseek-v3", "", "default"):
            return "deepseek-reasoner"
        return m


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

    def chat_messages(self, messages: list) -> str:
        url = f"{self.cfg.base_url()}/chat/completions"
        body = {
            "model": self.cfg.effective_model(),
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "messages": messages,
        }
        # DeepSeek / OpenAI-compatible thinking hints (ignored if unsupported)
        if self.cfg.thinking:
            body["thinking"] = {"type": "enabled"}
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
            err = e.read().decode("utf-8", "replace")[:300]
            # retry once without thinking field if provider rejects it
            if self.cfg.thinking and ("thinking" in err or e.code == 400):
                body.pop("thinking", None)
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.cfg.api_key()}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as resp:
                        raw = resp.read().decode("utf-8")
                except Exception as e2:  # noqa: BLE001
                    err2 = getattr(e2, "read", lambda: b"")()[:300]
                    raise LLMError(f"LLM request failed: {e2} {err2!r}") from e2
            else:
                raise LLMError(f"LLM HTTP {e.code}: {err}") from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM request failed: {e}") from e
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            msg = choice["message"] or {}
            content = msg.get("content")
            # thinking models: CoT in reasoning_content
            self.last_reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            self.last_finish_reason = choice.get("finish_reason")
            self.last_usage = data.get("usage") or {}
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM response parse failed: {e}") from e
        self.last_latency = time.time() - t0
        if not content:
            # some reasoner APIs put answer in content after long CoT
            if self.last_reasoning:
                content = self.last_reasoning
        if not content:
            raise LLMError(
                f"LLM empty content (finish_reason={getattr(self, 'last_finish_reason', None)})"
            )
        return str(content)
