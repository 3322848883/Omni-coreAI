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
| `manage_positions` | | 默认 `true` | `false` = 只开仓不管仓（持仓/保护单不注入快照，close/reduce/modify_tp_sl/trail 全拒）。**阶梯/网格 bot 勿用**（依赖 modify_tp_sl + resync 维护 SL 归并） |
| `allow_close` / `allow_reduce` | | 默认 `true` | 细粒度闸门：只禁平仓 / 只禁减仓（改保护放行）。**只作用于 AI 每轮的指令**，不作用于程序执行路径 |
| `account_risk.auto_protect` | 可选 | 默认关 | `dry` = 只记录「本来会挂什么」；`true` = 裸仓补 SL（**张数恒 = 持仓全量**；SL 只覆盖一部分时**只告警不补差额**——差额补单会造成部分出场） |
| `account_risk.give_back` | 可选 | 默认关 | 回调锁盈/保本平仓的**单独开关**（阈值由 AI 每笔填 `give_back_pct`、程序 60s 扫描执行）；不受 `allow_close` 影响 |
| `strategist.prompt_file` | 可选 | `prompts/xxx.md` | 只跑外部信号可删 strategist 段 |

**多账户**：不同 bot 写不同 `api_key_env`（如 `GATE_KEY_A` / `GATE_KEY_B`）。  
**同账户多策略**：相同 `api_key_env`，**不同** `bot_id` + **不同** `label_prefix` + 各自 inbox。

> 📖 **完整键清单见 [`config-reference.md`](config-reference.md)**（自动生成，与
> `omnialpha/config_keys.py` 的白名单同步）。写错键名会在加载时**告警**：
> 白名单之外的键一律提示「可能被静默忽略（typo？）」。改了白名单记得重跑
> `python scripts/gen_config_reference.py`（有漂移测试守着）。

### 3) 准备密钥环境变量

```powershell
# 名字必须与 yaml 里 api_key_env / api_secret_env 一致
$env:GATE_TESTNET_API_KEY    = "..."
$env:GATE_TESTNET_API_SECRET = "..."
```

**本地模拟盘（`env: paper`）不需要交易所密钥**——只需 `paper.feed_exchange`（行情来源）与 `paper.initial_capital` / `leverage` 等虚拟账户参数。启动用 `python -m omnialpha paper-run --bot <id>`（自带撮合/强平/费率 tick）。详见 README「本地模拟盘」。

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
python -m omnialpha once --bot <bot_id>     # 单次处理 inbox
python -m omnialpha run --bot <bot_id>      # 常驻执行
python -m omnialpha plan --bot <bot_id>     # 若启用 LLM，单轮 Plan

# 或全部 enabled 一起跑
python -m omnialpha run
```

---

## 验收清单（每加一个 bot 跑一遍）

| # | 检查 | 命令 / 预期 |
|---|------|-------------|
| 1 | 配置被识别 | `python -m omnialpha status` 里出现 `<bot_id>` |
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
| **查不到「谁平的仓 / 谁挂的止损」** | 程序 sweep 直接调交易所、不经过 `execute_signal`，本地日志原先没有它 | 看 `data/bots/<bot_id>/logs/trades.jsonl` 的 `source` 字段：`watcher`=信号执行、`watcher:give_back`=程序回调锁盈全平、`watcher:auto_protect`=程序兜底补 SL；交易所侧 `position_close` 的 `text` 只对「本 bot 条件单触发」可靠（`ao-<id>` 且 id 在 `trades.jsonl` 里），`api`/`app`/`web` 分别代表程序主动单 / 手动 |

---

**相关**：`AGENTS.md`（总览）· `README.md`「多机器人怎么加」· `templates/README.md`（信号字段）


## 一信号多所（信号广播）

一个 bot 只绑一个交易所；要一条信号同时在多所执行，用**广播**：

1. 建 N 个 bot 配置（各写自己的 `exchange:` + 密钥）
2. `config/broadcast.yaml` 定路由：`from: 源` → `to: [目标1, 目标2, ...]`（任意子集）
3. 信号投源 inbox，`python -m omnialpha broadcast` 自动分发到各目标

目标列表只在配置里写（AI 信号碰不到），详见 README「信号广播」。


## 多人格共管一个订单

N 个策略人格共同决策一笔订单（共识/投票/主人格裁决）：

1. 各成员 bot 配置照常（各自 `strategist` prompt）
2. `config/persona_groups.yaml` 定组：`members` + `topology` + `fusion`
3. `python -m omnialpha persona-run --group <name>` 常驻

拓扑：`single_account`（N 人格→1 账户一单）/ `mirror_accounts`（N 人格→N 账户同步）。
详见 README「多人格共管订单」。
