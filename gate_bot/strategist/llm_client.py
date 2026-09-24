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
    max_tokens: int = 2048
    default_base_url: str = "https://api.deepseek.com/v1"

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


class LLMClient:
    def __init__(self, cfg: Optional[LLMConfig] = None):
        self.cfg = cfg or LLMConfig()

    def chat(self, system: str, user: str) -> str:
        url = f"{self.cfg.base_url()}/chat/completions"
        body = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
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
            raise LLMError(f"LLM HTTP {e.code}: {err}") from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM request failed: {e}") from e
        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM response parse failed: {e}") from e
        self.last_latency = time.time() - t0
        return str(content)
