# LLM 供应商管理（Provider Registry）

**目标**：一处配置多供应商/多模型；bot 只写 `llm.provider`，工具层不变。

## 1. 配置

`config/providers.yaml`：

```yaml
default: deepseek-official

providers:
  deepseek-official:
    base_url: https://api.deepseek.com/v1
    api_key_env: OPENAI_API_KEY
    model: deepseek-flash
    thinking: true
    reasoning_effort: max
    timeout_sec: 120
    max_tokens: 8192
    user_id_prefix: ""          # 实际 user_id = prefix + bot_id（可空）
    json_mode: true

  gw-flash:
    base_url: http://69.12.85.185:7863/v1
    api_key_env: OPENAI_API_KEY
    model: global:deepseek-v4.1-flash
    thinking: false
    timeout_sec: 120
    max_tokens: 8192
    json_mode: true
```

## 2. bot 引用

```yaml
llm:
  provider: gw-flash          # 必选或 default
  model: ...                  # 可选覆盖
  thinking: false             # 可选覆盖
```

兼容：无 `provider` 时用 `llm` 原字段（或 `default` provider）。

## 3. 代码

| 文件 | 职责 |
|------|------|
| `gate_bot/providers.py` | 加载 yaml、`resolve_llm_config(root, bot_id, llm_dict)` |
| `__main__` | `_build_plan_runner` 改调 resolve |
| `config/providers.yaml` | 预设：官方 DeepSeek + 网关 |

解析优先级：`llm.*` 显式字段 > provider 预设 > 代码默认。

## 4. 工具

不改：OpenAI 兼容 `tools` + `json_mode` + thinking 字段，各 provider 自适配（已支持）。

## 5. 验收

- [ ] 单测：resolve 覆盖、default、缺 provider 报错/回退  
- [ ] `plan --bot eth-range-gw` 走网关  
- [ ] `plan --bot brooks-btc` 可切 `provider: deepseek-official`  
