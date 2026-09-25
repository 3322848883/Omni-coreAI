import sys
sys.path.insert(0, ".")
from gate_bot.strategist.llm_client import LLMClient, LLMConfig

cfg = LLMConfig(model="deepseek-flash", thinking=True, reasoning_effort="max", timeout_sec=120)
print("model", cfg.effective_model(), "max_tokens", cfg.effective_max_tokens())
c = LLMClient(cfg)
t = c.chat("只输出JSON", '{"ok":true}')
print("resp_model", getattr(c, "last_model", None))
print("content", t[:120])
print("reasoning_len", len(getattr(c, "last_reasoning", "") or ""))
print("usage", c.last_usage)
