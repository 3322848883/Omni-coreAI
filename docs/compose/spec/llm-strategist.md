---
feature: llm-strategist
status: delivered
updated: 2026-03-02
branch: master
commits: 58d5281..27b5e82
---

# LLM Strategist — AI 生成方案 + 程序执行

## Report

**What was built** — `gate_bot/strategist`：OpenAI 兼容 LLM 客户端、VergeX 式 Plan/chips 解析、行情+账户快照、策略 prompt（`prompts/vergex_default.md`）、**每 bot 独立风控**（min_confidence / max_notional / allow_actions / max_chips）、chips→现有 SignalFile 写 `inbox`、`plan` / `plan-loop` CLI（interval + K 线收盘触发、cycle 去重）。执行仍走 watcher/executor，复用 position_policy / replace。

**Verification** — `python -m unittest discover -s tests`：**59 PASS**（含 plan 解析、风控矩阵、bridge→parse_signal、LLM mock）。`python -m gate_bot --help` 含 plan/plan-loop；`from gate_bot.strategist import write_hold_audit` 可导入；独立复审确认 critical 全部关闭。

**Journey log** —
1. 先落地 chips 契约再写执行桥，保证输出可被 `parse_signal` 消费。
2. 复审指出 `write_hold_audit` 缺失会使 plan-loop 起不来——测试未覆盖 CLI import；补函数 + import 冒烟。
3. 超限 `size_usd` 按规格改为**拒绝**（勿静默截断）。
4. prompt 用字符串拼接避免 JSON 花括号被 `.format` 吃掉。

## Tasks

- [x] T1: `strategist/schema.py` — Plan/chip 解析与校验 — acceptance: 合法/非法 Plan 单测通过 (covers: S2)
- [x] T2: `strategist/llm_client.py` — OpenAI 兼容 chat.completions — acceptance: mock 响应解析成功/超时报错 (covers: S2)
- [x] T3: `strategist/snapshot.py` — 行情+账户快照组装 — acceptance: mock gate_client 产出 market/account/policy JSON (covers: S2)
- [x] T4: `strategist/prompt.py` + `prompts/vergex_default.md` — prompt 组装 — acceptance: 含 snapshot+风控约束+输出 schema (covers: S2)
- [x] T5: `strategist/risk.py` — 按 bot risk 过滤/截断 chips — acceptance: 风控矩阵单测 (covers: S2)
- [x] T6: `strategist/bridge.py` — chips→SignalFile 写 inbox — acceptance: 产出可被 parse_signal 执行的 JSON (covers: S2)
- [x] T7: CLI `plan` / `plan-loop`（interval + kline close） — acceptance: once 写文件；loop 可启动停止 (covers: S2)
- [x] T8: README 策略层文档 + 全量单测 — acceptance: unittest 全绿；README 含 Plan schema (covers: S2)

`gate-signal-bot` 只能执行**外部**写入的 JSON 信号。需要内置 LLM 策略层：按周期/事件采集行情与账户快照，由 AI 生成 **VergeX 式多品种决策**，经**每 bot 独立风控**后转成现有 signal JSON，交给执行器下单，形成完整策略交易闭环。

## [S2] Design

### 架构

```text
┌─────────────────────────────────────────────┐
│  strategist（新增，每 bot 一份）              │
│  collect snapshot → prompt → LLM → Plan     │
│       → risk gate → signal JSON             │
└──────────────────┬──────────────────────────┘
                   ▼  写入
            inbox/<bot_id>/*.json
                   ▼
         现有 watcher / executor（不变）
                   ▼
              Gate futures API
```

- **只写 inbox、不直接下单**：复用 position_policy / replace / 风控 / 归档。
- 借鉴 nofx/kernel：prompt 组装、双语数据摘要、chips 决策、cycle 审计。

### LLM 输出契约（Plan，仅此一种）

```json
{
  "cycle_id": "2026-03-02T15:30:00Z",
  "reasoning": "简短中文推理",
  "chips": [
    {
      "symbol": "BTC_USDT",
      "action": "open_long|open_short|add_long|add_short|reduce_long|reduce_short|close|close_all|hold|stop_entry_long|stop_entry_short",
      "confidence": 0.85,
      "size_usd": 50,
      "tp": 75000,
      "sl": 72000,
      "type": "market|limit|post_only|ioc|fok",
      "price": null,
      "reasoning": "..."
    }
  ]
}
```

- 解析失败 / 非 JSON → 本轮 hold，记日志，**不写 inbox**（不乱下单）
- 解析器兼容 code fence / 前后杂文 / 平衡花括号提取（reasoning 模型可能带思考痕迹）
- `hold` chip 不产生下单文件（可写 hold 审计文件）
- 通过风控后映射为 SignalFile：`orders[]` + `meta.plan_cycle` + `replace`

### 输入快照（进 prompt）

| 块 | 内容 | 来源 |
|----|------|------|
| market | 每 symbol：last、N 根 OHLCV（默认 60 @ timeframe） | Gate 公开 REST |
| account | available/total、position_mode、持仓 | `gate_client` |
| policy | 本 bot risk 与允许 action | bot 配置 |
| universe | `strategist.symbols` | bot 配置 |

### 机器人配置扩展（每 bot 独立风控）

```yaml
strategist:
  enabled: true
  interval_sec: 300          # 定时触发
  timeframe: 15m             # 事件触发：该周期 K 线收盘时再跑一轮
  event_on_kline_close: true
  symbols: [BTC_USDT, ETH_USDT]
  prompt_file: prompts/vergex_default.md
  write_hold: true
  risk:
    min_confidence: 0.75
    max_notional_usd: 50
    max_chips: 3
    allow_actions: [open_long, open_short, reduce_long, reduce_short, close, hold, stop_entry_long, stop_entry_short]
  llm:
    base_url_env: OPENAI_BASE_URL
    api_key_env: OPENAI_API_KEY
    model: deepseek-chat
    temperature: 0.2
    timeout_sec: 60
```

- **风控属于策略**：每 bot 一套 `risk`；与 bot 级 `max_notional_usd` / `position_policy` / `default_replace` 取更严。
- 触发：`interval_sec` 定时 + `event_on_kline_close`（K 线收盘事件），同一 cycle_id 去重。
- 触发细节：每秒轮询；定时优先于同秒的收盘事件；`_kline_closed` 以首个 symbol 的 `timeframe` K 线 `t` 前进为准；`run_once` 非重入；账户失败 abort。

### 风控（写 inbox 前强制）

| 规则 | 行为 |
|------|------|
| `confidence < min_confidence` | 该 chip 降级为 hold |
| `size_usd > max_notional_usd` | 拒或截断到上限（默认拒，记 reject） |
| `action ∉ allow_actions` | 拒 |
| `chips` 数 > `max_chips` | 只保留 confidence 最高的前 N |
| 无仓拒新方案 | 由执行器 `position_policy` 已有逻辑把关 |
| replace | plan 默认 `replace: "symbol"` 防堆积 |

### CLI / 进程

```text
python -m gate_bot plan --bot <id>            # 跑一轮：采集→LLM→写 inbox
python -m gate_bot plan-loop --bot <id>       # 常驻：interval + kline-close
```

与 `run`（watcher）可同机分进程：planner 只写文件，watcher 只执行。

### 错误行为

| 情况 | 行为 |
|------|------|
| LLM 超时/非 JSON | 记日志，本轮 hold，不写 inbox |
| 风控全拒 | `write_hold` 时写 hold 文件作审计 |
| 快照失败 | 跳过本轮，下周期重试 |

### 测试边界

- Plan 解析 / 非法 JSON / chips 校验（单测）
- 风控矩阵（单测）
- chips→SignalFile 映射（单测）
- LLM 客户端 mock HTTP（单测）
- 真实 LLM 仅 testnet 手工冒烟，不进 CI

## [S3] Out of Scope

- 不改执行层下单语义；不做 Web UI
- 不回测、不自动调参
- 不多账户跟单/资管
- 不做中文 action 名
- trail 资金密码问题仍搁置

## Tasks

- [ ] T1: `strategist/schema.py` — Plan/chip 解析与校验 — acceptance: 合法/非法 Plan 单测通过 (covers: S2)
- [ ] T2: `strategist/llm_client.py` — OpenAI 兼容 chat.completions — acceptance: mock 响应解析成功/超时报错 (covers: S2)
- [ ] T3: `strategist/snapshot.py` — 行情+账户快照组装 — acceptance: mock gate_client 产出 market/account/policy JSON (covers: S2)
- [x] T4: `strategist/prompt.py` + `prompts/vergex_default.md` — prompt 组装 — acceptance: 含 snapshot+风控约束+输出 schema (covers: S2)（固定契约 + 可换 `prompt_file` 人格，见 `prompts/README.md`）
- [ ] T5: `strategist/risk.py` — 按 bot risk 过滤/截断 chips — acceptance: 风控矩阵单测 (covers: S2)
- [ ] T6: `strategist/bridge.py` — chips→SignalFile 写 inbox — acceptance: 产出可被 parse_signal 执行的 JSON (covers: S2)
- [x] T7: CLI `plan` / `plan-loop`（interval + kline close） — acceptance: once 写文件；loop 可启动停止 (covers: S2)
- [ ] T8: README 策略层文档 + 全量单测 — acceptance: unittest 全绿；README 含 Plan schema (covers: S2)
