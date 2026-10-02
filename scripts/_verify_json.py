import sys
sys.path.insert(0, ".")
from omnialpha.strategist.llm_client import LLMClient, LLMConfig

cfg = LLMConfig(model="deepseek-flash", thinking=True, reasoning_effort="max", timeout_sec=180, max_tokens=8192)
print("eff_max", cfg.effective_max_tokens())
c = LLMClient(cfg)
m = c.chat_message_full([{"role": "user", "content": '只输出JSON: {"ok":true}'}])
print("finish", c.last_finish_reason)
print("usage", c.last_usage)
print("content", repr(m.get("content"))[:80])
print("r_len", len(m.get("reasoning_content") or ""))
