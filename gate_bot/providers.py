"""LLM provider registry: one place to pick base_url/model/thinking per bot.

Bot yaml:
  llm:
    provider: deepseek-official
    model: deepseek-flash   # optional override
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from .strategist.llm_client import LLMConfig


class ProviderError(ValueError):
    pass


def providers_path(root: Path) -> Path:
    return Path(root).resolve() / "config" / "providers.yaml"


def load_providers(root: Path) -> dict[str, Any]:
    import yaml

    p = providers_path(root)
    if not p.is_file():
        return {"default": "", "providers": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {
        "default": str(data.get("default") or ""),
        "providers": dict(data.get("providers") or {}),
    }


def resolve_llm_config(
    root: Path,
    bot_id: str = "",
    llm: Optional[dict] = None,
) -> LLMConfig:
    """Build LLMConfig from providers.yaml + optional per-bot overrides."""
    llm = dict(llm or {})
    reg = load_providers(root)
    name = str(llm.get("provider") or "").strip()
    if not name:
        name = reg.get("default") or ""
    prov = dict((reg.get("providers") or {}).get(name) or {}) if name else {}
    if name and name not in (reg.get("providers") or {}) and not _has_inline(llm):
        raise ProviderError(
            f"unknown llm.provider {name!r}; known: {sorted((reg.get('providers') or {}))}"
        )

    def pick(key, default=None):
        if key in llm and llm.get(key) is not None and llm.get(key) != "":
            return llm.get(key)
        return prov.get(key, default)

    base_url = pick("base_url") or "https://api.deepseek.com/v1"
    api_key_env = str(pick("api_key_env") or "OPENAI_API_KEY")
    model = str(pick("model") or "deepseek-flash")
    thinking = bool(pick("thinking", True))
    effort = str(pick("reasoning_effort") or "high")
    timeout = int(pick("timeout_sec") or 120)
    max_tokens = int(pick("max_tokens") or 8192)
    json_mode = bool(pick("json_mode", True))
    user_id = str(pick("user_id") or "")
    if not user_id and (prov.get("user_id_prefix") or llm.get("user_id_prefix") or bot_id):
        prefix = str(pick("user_id_prefix") or "")
        user_id = f"{prefix}{bot_id}" if (prefix or bot_id) else ""

    # env can override base_url if provider left it empty path style
    return LLMConfig(
        base_url_env=str(pick("base_url_env") or "OPENAI_BASE_URL"),
        api_key_env=api_key_env,
        model=model,
        temperature=float(pick("temperature") or 0.2),
        timeout_sec=timeout,
        max_tokens=max_tokens,
        default_base_url=str(base_url),
        thinking=thinking,
        reasoning_effort=effort,
        user_id=user_id,
        json_mode=json_mode,
    )


def _has_inline(llm: dict) -> bool:
    return bool(llm.get("model") or llm.get("default_base_url") or llm.get("base_url"))


def describe(root: Path) -> dict:
    reg = load_providers(root)
    out = {"default": reg.get("default"), "providers": []}
    for k, v in (reg.get("providers") or {}).items():
        out["providers"].append({
            "name": k,
            "label": v.get("label"),
            "model": v.get("model"),
            "base_url": v.get("base_url"),
            "thinking": v.get("thinking"),
        })
    return out
