# 如何添加一个新机器人（照抄清单）

一个机器人 = **一份 yaml + 一个 inbox 目录**。按下面 6 步做完，再跑「验收」。

---

## 6 步操作

### 1) 复制配置模板

```powershell
Copy-Item config\bots\_example.yaml config\bots\<bot_id>.yaml
# 例：config\bots\alpha2.yaml
```

### 2) 改最小必填项

打开 `config/bots/<bot_id>.yaml`：

| 字段 | 必填 | 改成什么 | 注意 |
|------|------|----------|------|
| `bot_id` | ✅ | 与文件名一致，如 `alpha2` | 全局唯一 |
| `env` | ✅ | `testnet` 或 `live` | 先 testnet \| paper |
| `api_key_env` / `api_secret_env` | ✅ | 环境变量**名字**（不是密钥） | 多账户时各 bot 不同名 |
| `symbols` | ✅ | 该 bot 允许交易的币 | 白名单 |
| `max_notional_usd` | ✅ | 单笔名义上限 | 首日建议 ≤10（live） |
| `label_prefix` | ✅ | 短前缀，如 `a2` | **必须与其他 bot 不同**，防误撤 |
| `enabled` | | `true` | `false` 表示停用 |
| `require_sl` | | 默认 `true` | 保持 true |
| `order_scope` | | 默认 `own` | 多 bot 共账户必须 `own` |
| `strategist.prompt_file` | 可选 | `prompts/xxx.md` | 只跑外部信号可删 strategist 段 |

**多账户**：不同 bot 写不同 `api_key_env`（如 `GATE_KEY_A` / `GATE_KEY_B`）。  
**同账户多策略**：相同 `api_key_env`，**不同** `bot_id` + **不同** `label_prefix` + 各自 inbox。

### 3) 准备密钥环境变量

```powershell
# 名字必须与 yaml 里 api_key_env / api_secret_env 一致
$env:GATE_TESTNET_API_KEY    = "..."
$env:GATE_TESTNET_API_SECRET = "..."
```

**本地模拟盘（`env: paper`）不需要交易所密钥**——只需 `paper.feed_exchange`（行情来源）与 `paper.initial_capital` / `leverage` 等虚拟账户参数。启动用 `python -m gate_bot paper-run --bot <id>`（自带撮合/强平/费率 tick）。详见 README「本地模拟盘」。

### 4) 确认 inbox 目录

首次 `status` / `run` 会自动创建 `inbox/<bot_id>/`。也可手动：

```powershell
New-Item -ItemType Directory -Force inbox\<bot_id>
```

信号源（人 / AI / 脚本）**只往这个目录丢 JSON**：

```text
inbox/<bot_id>/20260101-120000-signal.json
```

### 5)（可选）给该 bot 配策略人格

```yaml
strategist:
  enabled: true
  prompt_file: prompts/multi_breakout.md   # 或自建 prompts/my.md
  interval_sec: 300
  timeframe: 15m
  symbols: [BTC_USDT]                      # 可与顶层 symbols 一致
  risk:
    min_confidence: 0.75
    max_chips: 1
    max_notional_usd: 30
```

纯外部信号 bot 可**整段去掉** `strategist:`。

### 6) 启动方式

```powershell
# 先只跑这一个 bot
python -m gate_bot once --bot <bot_id>     # 单次处理 inbox
python -m gate_bot run --bot <bot_id>      # 常驻执行
python -m gate_bot plan --bot <bot_id>     # 若启用 LLM，单轮 Plan

# 或全部 enabled 一起跑
python -m gate_bot run
```

---

## 验收清单（每加一个 bot 跑一遍）

| # | 检查 | 命令 / 预期 |
|---|------|-------------|
| 1 | 配置被识别 | `python -m gate_bot status` 里出现 `<bot_id>` |
| 2 | 环境正确 | `env` 是 testnet/live，与密钥一致 |
| 3 | 隔离前缀 | `label_prefix` 与其他 bot **不重复** |
| 4 | 冒烟信号 | 投一个最小 JSON（见下）→ `once --bot <bot_id>` 成功 |
| 5 | 归档位置 | `archive/done/<bot_id>/` 或失败在 `archive/failed/<bot_id>/` |
| 6 | 日志有流水 | `logs/trades/<bot_id>.jsonl` 有记录 |
| 7 | 无残留误伤 | 其他 bot 的挂单未被撤掉（order_scope=own） |

**冒烟信号示例**（testnet，价位请改成当时可接受区间）：

```json
{
  "action": "open_long",
  "symbol": "BTC_USDT",
  "size_usd": 15,
  "type": "market",
  "tp": 200000,
  "sl": 1000,
  "label": "smoke"
}
```

---

## 三种常见组合（对照）

| 盺景 | 配置要点 |
|------|----------|
| **新账户 + 新策略** | 新 `api_key_env` + 新 `bot_id` + 新 `label_prefix` + 新 inbox |
| **同账户 + 新策略** | `api_key_env` 照抄；**换** `bot_id` / `label_prefix` / inbox |
| **同策略 + 新账户** | `prompt_file` 照抄；**换** 密钥 env + `bot_id` / `label_prefix` |

更多 yaml 片段见 `examples/bots/README.md`（单订单 / 网格 / 突破）。

---

## 常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| `status` 看不到 bot | 文件不在 `config/bots/` 或 yaml 语法错 | 检查缩进、文件名 |
| `credentials: ...` | 环境变量名对不上或未设置 | 对照 `api_key_env` |
| 信号不被执行 | 写错目录 | 必须是 `inbox/<bot_id>/` |
| 误撤别的 bot 挂单 | `label_prefix` 撞了或 `order_scope: all` | 前缀唯一；保持 `own` |
| `symbol ... not in whitelist` | 币不在 `symbols` | 加白名单 |
| LLM 找不到 prompt | `prompt_file` 路径不对 | 相对项目根 `prompts/...` |

---

**相关**：`AGENTS.md`（总览）· `README.md`「多机器人怎么加」· `templates/README.md`（信号字段）


## 一信号多所（信号广播）

一个 bot 只绑一个交易所；要一条信号同时在多所执行，用**广播**：

1. 建 N 个 bot 配置（各写自己的 `exchange:` + 密钥）
2. `config/broadcast.yaml` 定路由：`from: 源` → `to: [目标1, 目标2, ...]`（任意子集）
3. 信号投源 inbox，`python -m gate_bot broadcast` 自动分发到各目标

目标列表只在配置里写（AI 信号碰不到），详见 README「信号广播」。
