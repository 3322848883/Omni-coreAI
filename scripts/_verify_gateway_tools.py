"""Test tools + plan with gateway model global:deepseek-v4.1-flash."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
# 密钥从环境变量来（勿硬编码）；跑前先设 OPENAI_API_KEY / OPENAI_BASE_URL
os.environ.setdefault("OPENAI_BASE_URL", "http://69.12.85.185:7863/v1")
os.environ.setdefault("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
os.environ["LLM_MODEL"] = "global:deepseek-v4.1-flash"

from omnialpha.config import load_bot_config
from omnialpha.gate_client import GateClient
from omnialpha.strategist.llm_client import LLMClient, LLMConfig
from omnialpha.strategist.prompt import build_system_prompt, load_strategy_prompt
from omnialpha.strategist.tools import NATIVE_TOOLS, TOOL_GUIDE, run_tool

bot = load_bot_config(Path("config/bots/eth-range-gw.yaml"))
raw = dict(bot.strategist.get("llm") or {})
print("yaml_llm", {k: raw.get(k) for k in ("model", "thinking", "timeout_sec")})

cfg = LLMConfig(
    base_url_env="OPENAI_BASE_URL",
    api_key_env="OPENAI_API_KEY",
    model=str(raw.get("model") or "global:deepseek-v4.1-flash"),
    thinking=bool(raw.get("thinking", False)),
    timeout_sec=120,
    max_tokens=8192,
)
print("base", cfg.base_url(), "model", cfg.effective_model(), "thinking", cfg.thinking)

system = build_system_prompt(load_strategy_prompt("prompts/eth_range_mid.md"), tools_guide=TOOL_GUIDE)
user = "查 ETH_USDT ticker 与 account，然后输出 Plan JSON（可 hold）。"
llm = LLMClient(cfg)

msg = llm.chat_message_full(
    [{"role": "system", "content": system}, {"role": "user", "content": user}],
    tools=NATIVE_TOOLS,
    tool_choice="auto",
)
print("resp_model", llm.last_model)
print("finish", llm.last_finish_reason)
print("tool_calls", [((t.get("function") or {}).get("name")) for t in msg.get("tool_calls") or []])
print("reasoning_len", len(msg.get("reasoning_content") or ""))
print("usage", llm.last_usage)

client = GateClient(os.environ.get("GATE_TESTNET_API_KEY", ""), os.environ.get("GATE_TESTNET_API_SECRET", ""), env="testnet")
if msg.get("tool_calls"):
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
        {
            "role": "assistant",
            "content": msg.get("content") or "",
            "reasoning_content": msg.get("reasoning_content") or "",
            "tool_calls": msg["tool_calls"],
        },
    ]
    for tc in msg["tool_calls"]:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        result = run_tool(client, fn.get("name"), args, env="testnet", bot_root=Path("."))
        print("TOOL", fn.get("name"), "->", str(result)[:140])
        messages.append({"role": "tool", "tool_call_id": tc.get("id") or "",
                         "content": json.dumps(result, ensure_ascii=False)[:2000]})
    final = llm.chat_message_full(messages)
    # second pass if not JSON
    text = final.get("content") or ""
    if not text.strip().startswith("{"):
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": "禁止再调用工具。只输出 Plan JSON。"})
        final = llm.chat_message_full(messages)
        text = final.get("content") or ""
    print("final_r_len", len(final.get("reasoning_content") or ""))
else:
    text = msg.get("content") or ""

print("final_head", text[:220].replace("\n", " "))
print("looks_json", text.strip().startswith("{"))
print("GW_TOOLS_VERIFY_DONE")
