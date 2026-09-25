"""Minimal verify: DeepSeek flash + thinking max + native tools + plan path."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
os.environ.setdefault("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

from gate_bot.config import load_bot_config
from gate_bot.gate_client import GateClient
from gate_bot.strategist.llm_client import LLMClient, LLMConfig
from gate_bot.strategist.prompt import build_system_prompt, load_strategy_prompt
from gate_bot.strategist.tools import NATIVE_TOOLS, TOOL_GUIDE, run_tool

bot = load_bot_config(Path("config/bots/brooks-btc.yaml"))
raw_llm = dict(bot.strategist.get("llm") or {})
print("yaml", {k: raw_llm.get(k) for k in ("model", "thinking", "reasoning_effort", "user_id", "max_tokens")})

cfg = LLMConfig(
    model=str(raw_llm.get("model") or "deepseek-flash"),
    thinking=bool(raw_llm.get("thinking", True)),
    reasoning_effort=str(raw_llm.get("reasoning_effort") or "max"),
    user_id=str(raw_llm.get("user_id") or ""),
    timeout_sec=120,
    max_tokens=int(raw_llm.get("max_tokens") or 8192),
)
print("effective", cfg.effective_model(), "thinking", cfg.thinking, "effort", cfg.reasoning_effort)

system = build_system_prompt(load_strategy_prompt("prompts/brooks_btc_pa.md"), tools_guide=TOOL_GUIDE)
print("system_json_sample", "cycle_id" in system)

llm = LLMClient(cfg)
user = "查 BTC_USDT ticker 后输出 Plan JSON（字段含 chips）。"
msg = llm.chat_message_full(
    [{"role": "system", "content": system}, {"role": "user", "content": user}],
    tools=NATIVE_TOOLS,
    tool_choice="auto",
)
print("resp_model", llm.last_model)
print("tool_calls", [((t.get("function") or {}).get("name")) for t in msg.get("tool_calls") or []])
print("reasoning_len", len(msg.get("reasoning_content") or ""))

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
    client = GateClient(os.environ.get("GATE_API_KEY", ""), os.environ.get("GATE_API_SECRET", ""), env="live")
    for tc in msg["tool_calls"]:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        result = run_tool(client, fn.get("name"), args, env="live")
        print("tool", fn.get("name"), "->", str(result)[:100])
        messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id") or "",
            "content": json.dumps(result, ensure_ascii=False)[:2000],
        })
    final = llm.chat_message_full(messages)
    text = final.get("content") or ""
    print("final_reasoning_len", len(final.get("reasoning_content") or ""))
else:
    text = msg.get("content") or ""

print("final_head", text[:200].replace("\n", " "))
print("looks_json", text.strip().startswith("{"))
print("VERIFY_OK")
