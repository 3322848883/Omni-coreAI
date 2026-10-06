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
    """Build LLMConfig from providers.yaml + optional per-bot overrides.

    同时解析**备用供应商链**（见 `_resolve_fallbacks`）：主供应商不可用时按顺序切，
    而不是让整轮决策降级 hold。
    """
    llm = dict(llm or {})
    reg = load_providers(root)
    providers = dict(reg.get("providers") or {})
    name = str(llm.get("provider") or "").strip() or str(reg.get("default") or "")
    prov = dict(providers.get(name) or {}) if name else {}
    if name and name not in providers and not _has_inline(llm):
        raise ProviderError(
            f"unknown llm.provider {name!r}; known: {sorted(providers)}"
        )

    cfg = _build_config(name, prov, llm, bot_id)
    cfg.fallbacks = _resolve_fallbacks(name, providers, prov, llm, bot_id)
    return cfg


# 备用链的最大长度。防「A→B→C→D…」这种越切越慢的配置：
# 每个备用都要先耗完主供应商的重试退避（3 次约 10.5s）才轮到它。
MAX_FALLBACKS = 3


def _fallback_names(prov: dict, llm: dict) -> list[str]:
    """备用供应商名列表。bot 级 `llm.fallback` 优先于供应商级 `fallback`。

    两者都接受单个名字（字符串）或名字列表。
    """
    raw = llm.get("fallback")
    if raw is None:
        raw = prov.get("fallback")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ProviderError(
            f"llm.fallback 必须是名字或名字列表，收到 {type(raw).__name__}"
        )
    return [str(x).strip() for x in raw if str(x).strip()]


def _resolve_fallbacks(name: str, providers: dict, prov: dict,
                       llm: dict, bot_id: str) -> list[LLMConfig]:
    """递归展开备用链（A→B→C），去重 + 防环 + 限长。

    **备用配置只取供应商自己的设置**（`llm={}`）——bot 级的 `model` 覆盖是针对
    主供应商的，套到备用上会把备用改成和主供应商同一个模型，回退就失去意义了。

    指向不存在的供应商**直接报错**（而不是静默跳过）：配置写错却悄悄没有备用，
    正是本项目反复出现的「静默失效」形态 —— 而备用链恰恰是在出事时才被依赖的东西。
    """
    out: list[LLMConfig] = []
    seen = {name} if name else set()
    queue = _fallback_names(prov, llm)
    while queue and len(out) < MAX_FALLBACKS:
        nxt = queue.pop(0)
        if nxt in seen:
            continue
        seen.add(nxt)
        sub = dict(providers.get(nxt) or {})
        if not sub:
            raise ProviderError(
                f"llm.fallback 指向未知供应商 {nxt!r}; known: {sorted(providers)}"
            )
        out.append(_build_config(nxt, sub, {}, bot_id))
        queue.extend(_fallback_names(sub, {}))
    return out


def _build_config(name: str, prov: dict, llm: dict, bot_id: str) -> LLMConfig:
    """把「供应商条目 + bot 级覆盖」合成为一个 LLMConfig。"""

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
        name=name,
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
