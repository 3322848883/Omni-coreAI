import sys
sys.path.insert(0, ".")
from omnialpha.strategist.llm_client import LLMClient, LLMConfig
from omnialpha.strategist.tools import NATIVE_TOOLS

cfg = LLMConfig(model="deepseek-flash", thinking=True, reasoning_effort="low",
                timeout_sec=120, max_tokens=4096)
c = LLMClient(cfg)
msg = c.chat_message_full(
    [
        {"role": "system", "content": "你是助手，可调用工具。最终只输出 JSON。"},
        {"role": "user", "content": "查 BTC_USDT ticker，然后输出 JSON 字段 last"},
    ],
    tools=NATIVE_TOOLS,
    tool_choice="auto",
)
print("tool_calls", [(t.get("function") or {}).get("name") for t in (msg.get("tool_calls") or [])])
print("reasoning_len", len(msg.get("reasoning_content") or ""))
print("content", (msg.get("content") or "")[:100])
