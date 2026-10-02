# 系统升级报告 — 多人格共管 + 记忆架构

> 日期: 2026-09-28 | 合并提交: 9ed2138 | 测试: 542 OK

---

## 一、升级总览

本次升级为 OmniAlpha 引入**多 AI 人格共管订单**与**四层记忆架构**，从"单 bot 独立决策"升级为"多策略合议决策 + 全生命周期记忆"。

| 功能 | 状态 | 测试 |
|------|------|------|
| 多人格共管（multi-persona） | ✅ | 449 项 |
| 记忆系统（agent-memory） | ✅ | 48 项 |
| 讨论模式（discussion） | ✅ | 13 项 |
| **全量** | ✅ | **542 项** |

---

## 二、新功能

### 2.1 多人格共管（multi-persona）

N 个策略人格共同管理订单，独立分析后融合执行。

| 融合模式 | 说明 | 适用场景 |
|----------|------|----------|
| `weighted_vote` | 权重过半才执行 | 多风格互补，防单一人格独走 |
| `master_arbiter` | 主人格裁决 | 有明确主力策略 |
| `consensus` | 共识分 ≥ 阈值 | 极度保守 |

| 拓扑 | 说明 |
|------|------|
| `single_account` | N 人格 → 1 账户（去重单） |
| `mirror_accounts` | N 人格 → N 账户同步 |

**配置**：`config/persona_groups.yaml`
**运行**：`python -m omnialpha persona-run --group <name>`

### 2.2 讨论模式（可选）

各人格互看 reasoning 后修正决策，基于 Du et al. 2023 研究。

```yaml
discussion:
  enabled: true
  rounds: 2            # 硬上限 4 轮
  early_exit_on_agreement: true
```

**防无限讨论**：硬轮次上限 + 提前终止 + 超时

### 2.3 记忆系统（agent-memory）

四层记忆，恒定 ~4k token/轮，月成本 ~$0.73/bot：

| 层 | 载体 | 功能 |
|----|------|------|
| **Order** | `data/shared/orders/` | 订单上下文 + top-5 关键事件 |
| **Journal** | `state/memory_journal.jsonl` | append-only 事件溯源（合规级审计） |
| **Profile** | `state/memory_profile.json` | 确定性策略画像 |
| **Working** | 每轮快照 + 近 3 轮 | 实时状态 |

**关键设计**：
- `memory_refs` — 决策引用历史索引（FinPos 模式，机器可校验）
- `recent_events` — top-5 关键决策事件（FinMem K=5 消融最优）
- `lifecycle` — 完整管理事件（MiFID II 级审计）
- `invalidation` — 旧假设失效标记（Memora FAMA）
- 缓存优化 — 稳定前缀 ~1850t 缓存命中（DeepSeek 1/50 价）

---

## 三、Bug 修复（5 个）

| Bug | 严重度 | 修复 |
|-----|--------|------|
| 信号文件同秒覆盖 | 高 | pid+ns 后缀唯一化 |
| schema 不兼容（close_long 等 3 个 action） | 高 | `_normalize_action` 归一化 |
| 多组隔离失效（list_open 不按 group 过滤） | 高 | group 过滤参数 |
| Windows 并发写竞态（PermissionError） | 中 | per-order threading.Lock |
| order_id 校验漏洞（null/unicode/`.`） | 中 | 收紧校验 |
| lifecycle_act 映射（stop_entry→hold） | 低 | 映射到 open |
| Journal/Profile 并发数据丢失 | 高 | threading.Lock |

---

## 四、测试覆盖

### 4.1 单元测试（542 项）

| 模块 | 测试数 | 覆盖 |
|------|--------|------|
| persona（含 multi-persona） | 449 | 功能/边界/安全/并发 |
| memory（记忆系统） | 48 | 功能/并发/崩溃/资源/安全 |
| discussion（讨论模式） | 13 | 配置/流程/上限/终止 |
| 其他（broadcast/paper/schema…） | 32 | 回归 |

### 4.2 真实 LLM 生产测试

| 场景 | 轮数 | 通过 | 验证 |
|------|------|------|------|
| weighted_vote（4 人格） | 6 | 6/6 | 投票/冲突/执行 |
| master_arbiter（2 人格） | 3 | 2/3 | 主人格裁决 |
| consensus（3 人格） | 2 | 2/2 | 共识阈值 |
| discussion（3 人格） | 6 | 6/6 | 讨论/修正/融合 |
| mirror_accounts | 2 | 2/2 | 镜像分发 |
| 不同策略人格碰撞 | 3 | 3/3 | Brooks/SMC/均值回归 |

### 4.3 生产级测试

| 类别 | 测试数 | 内容 |
|------|--------|------|
| 并发安全 | 8 | 多线程写入不丢数据 |
| 崩溃恢复 | 8 | 坏文件/临时文件/归档 |
| 安全防护 | 12 | 路径穿越/注入/泄露/越权 |
| 资源耗尽 | 8 | 1000 单/500 投票/100 信号 |
| 边界值 | 10 | NaN/Inf/unicode/空值 |

---

## 五、调研基础

本次设计基于 **113 条一手调研发现**（8+2 角度）：

| 领域 | 关键来源 | 应用 |
|------|----------|------|
| 厂商架构 | OpenAI Dreaming / Anthropic / MS | 精选注入、JIT 检索 |
| 开源框架 | MemGPT/Letta / Mem0 / Zep | 分层页迁、事实抽取 |
| 学术前沿 | LongMemEval / MemOps / FAMA | 遗忘一等公民、30% 衰减 |
| 交易系统 | MiFID II / TradingAgents / FinPos | 事件溯源、memory_refs |
| 上下文工程 | Manus / DeepSeek / Azure | 前缀稳定、缓存优化 |
| 失败反模式 | 1.2% 毒→0.85→0.30 | 写时准入门控 |

---

## 六、已知限制与后续

| 限制 | 影响 | 后续 |
|------|------|------|
| reason_text 未挂钩 Plan reasoning | 开仓理由为空 | T2 补全 |
| journal confidence 缺失 | conf=- | 写入时补 |
| profile 需平仓闭环 | 未平仓时统计为空 | paper 长跑观察 |
| 讨论模式 rounds=1 多 | 简单分歧早退出 | 复杂场景观察 |

---

## 七、向后兼容

- **单 BOT 模式**完全不受影响（原有命令照常）
- **讨论模式**默认关闭（不加配置行为不变）
- **记忆系统**自动启用（不影响现有执行流程）
- **合并无冲突**（ort 策略，542 测试全过）

---

*升级完成。模拟盘持续运行中，等验证后可切实盘。*
