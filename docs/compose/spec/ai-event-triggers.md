---
feature: ai-event-triggers
status: designed
updated: 2026-09-25
branch: master
commits: # 交付后填
---

# AI 自设事件触发（参数化唤醒）详细方案

## [S1] Problem

当前事件触发（`strategist.conditions[]`）只能写在 yaml 里，由用户预设。  
AI 分析行情后（如「跌破 83500 再来」「RSI 超买再看」）**无法**把这类唤醒条件登记到事件引擎；只能用一次性 `stop_entry_*` 交易所条件单，或等 `interval_sec` 定时。

目标：把触发做成**与定时同级的参数化唤醒源**——AI 可在权限内增删触发条件；**命中后跑一轮 Plan（AI 再分析）**，不直接下单。

## [S2] Design

### 2.1 核心语义

```text
触发条件（yaml 预设 + AI 动态）
        │ 命中（价格/指标）
        ▼
   跑一轮 Plan（AI 重新分析行情）
        ▼
   风控 → inbox → 执行器（可 hold / 可下单）
```

与 `interval_sec` / `event_on_kline_close` **同级**，不是下单指令。

### 2.2 权限模型（可配参数）

```yaml
strategist:
  ai_triggers:
    enabled: true
    allow_types: [price_break, price_vs_ema, ema_cross, macd_cross,
                  atr_spike, rsi, volume_spike]     # 白名单
    max_active: 5                 # AI 同时最多几条
    default_cooldown_sec: 60
    default_ttl_sec: 86400        # 24h 自动过期
    allow_modify: true            # AI 可增/删/替换自己的触发
    allow_symbols: []             # 空 = 用 bot.symbols
    limits:
      lookback: [5, 100]
      period: [2, 200]
      level: [1, 99]              # rsi
      mult: [1.0, 5.0]            # atr
      cooldown_sec: [10, 86400]
```

| 参数 | 作用 |
|------|------|
| `enabled` | 总开关（默认 false） |
| `allow_types` | AI 只能用这些触发类型 |
| `max_active` | 防刷爆 |
| `ttl_sec` | 过期自动删除，防永久挂条件 |
| `allow_modify` | false 则 AI 只能追加、不能删 |
| 数值 `limits` | 参数夹逼，防离谱阈值 |

**用户 yaml `conditions[]` 不受此限**（人工全权）；本方案只约束 **AI 动态触发**。

### 2.3 数据契约

#### A. Plan 输出（LLM）

```json
{
  "cycle_id": "...",
  "chips": [ ... ],
  "triggers": [
    {
      "type": "price_break",
      "symbol": "BTC_USDT",
      "lookback": 20,
      "side": "low",
      "cooldown_sec": 120,
      "ttl_sec": 3600,
      "reason": "跌破前低再评估空"
    },
    {
      "type": "rsi",
      "symbol": "BTC_USDT",
      "period": 14,
      "op": "gt",
      "level": 75
    }
  ],
  "trigger_ops": [
    {"op": "add", "ref": "…"},
    {"op": "remove", "id": "t-a1b2"},
    {"op": "replace_all"}
  ]
}
```

- `triggers[]`：新增；缺省 `trigger_ops` 视为 `add`
- `trigger_ops`：增删改（受 `allow_modify`）
- 校验失败条目 → 记入 `rejected_triggers`，**不阻断** chips

#### B. 运行时存储

`history/<bot_id>/ai_triggers.json`

```json
{
  "updated_at": "…",
  "items": [
    {
      "id": "t-a1b2c3",
      "type": "price_break",
      "symbol": "BTC_USDT",
      "lookback": 20,
      "side": "low",
      "cooldown_sec": 120,
      "ttl_sec": 3600,
      "created_at": 1790000000,
      "expire_at": 1790086400,
      "last_fire": 0,
      "reason": "…",
      "source": "llm"
    }
  ]
}
```

#### C. 合并进事件引擎

```text
active_conditions = yaml.conditions + ai_triggers.items（未过期）
plan-loop 每 check_interval_sec 检查 → 命中 → run_once(trigger=cond[...])
```

复用现有 `triggers.py`（`evaluate_condition` / `check_conditions`），不另起引擎。

### 2.4 校验规则（安全）

| 规则 | 行为 |
|------|------|
| type ∉ allow_types | 拒 |
| symbol ∉ bot.symbols | 拒 |
| 数值超 limits | 夹到边界并记 note（或拒，默认拒） |
| 超过 max_active | 拒新增，返回 `trigger_limit` |
| ttl 缺省 | 填 `default_ttl_sec` |
| 路径/注入 | id 由系统生成 `t-{8hex}`，AI 不可指定路径 |
| 命中后 | 只调 `run_once`，**不写订单** |

### 2.5 与 stop_entry 的分工

| | AI `triggers[]` | `stop_entry_*` |
|--|-----------------|----------------|
| 作用 | 唤醒 AI 分析 | 交易所条件开仓 |
| 命中后 | 重新 Plan（可 hold） | 直接开仓（有 SL/TP） |
| 生命周期 | TTL / 可删 | 挂到成交/撤单 |

策略可组合：先挂 `stop_entry` 做突破；同时设 `price_break` 触发让 AI 复核（双保险）。

### 2.6 落地改动

| 文件 | 改动 |
|------|------|
| `strategist/schema.py` | Plan 增加 `triggers` / `trigger_ops` 解析 |
| `strategist/trigger_store.py` | 新增：读写 `ai_triggers.json`，校验、TTL、id |
| `strategist/risk.py` 或新 `trigger_policy.py` | `ai_triggers` 白名单/上限/夹逼 |
| `strategist/loop.py` | run_once 后应用 trigger_ops；run_forever 合并条件 |
| `__main__.py` | 装配 `ai_triggers` 配置 |
| `config/bots/_example.yaml` | 示例块 |
| `tests/test_ai_triggers.py` | 校验/TTL/上限/合并/命中跑 Plan |

## [S3] Out of Scope

- 触发直接下单（明确不做，只唤醒）
- 自定义表达式 / 任意代码
- 跨 bot 共享触发
- 图形界面

## Tasks

- [ ] T1: `trigger_store.py` — 存储、id、TTL、增删改 — acceptance: 单测 (covers: S2.3B)
- [ ] T2: Plan schema `triggers`/`trigger_ops` + 策略校验 — acceptance: 非法 type/越界拒 (covers: S2.3A, S2.4)
- [ ] T3: loop 合并 yaml+ai 触发，命中 `run_once` — acceptance: mock 条件命中调用 Plan (covers: S2.1, S2.3C)
- [ ] T4: 配置装配 + 示例 yaml + 文档 — acceptance: README/templates (covers: S2.2)
- [ ] T5: 模拟盘：AI 输出 triggers → 落盘 → 构造命中 → 跑 Plan — acceptance: 脚本 PASS (covers: S2; depends: T1–T3)
