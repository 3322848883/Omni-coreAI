---
feature: multi-persona
status: delivered
updated: 2026-09-27
branch: feat/multi-persona
commits: 544a008..HEAD
---

# 多人格共管订单（Multi-Persona Order Management）

## Report

**What was built** — N 个策略人格（brooks / smc / scalper…）共同管理订单的机制。`config/persona_groups.yaml` 定义组：两种拓扑（`single_account` N 人格→1 账户一单去重执行 / `mirror_accounts` N 人格→N 账户原子同步）、三种融合（`weighted_vote` 加权投票 / `master_arbiter` 主人格裁决 / `consensus` 共识阈值）。各人格独立分析（各自 prompt/工具）→ 融合层合并成 1 个动作 → 按拓扑执行。共同记忆 `data/shared/orders/<order_id>.json` 跨 bot 记录订单状态/各人格理由/投票史。

**Verification** — `python -m unittest discover -s tests`：**329 PASS**（含 13 个 persona + 8 个关键修复回归）。端到端实测（真实 LLM，3 个 paper 人格）：votes short/short/hold → weighted_vote→short → single_account 写 target inbox；全 hold → 不执行。

**Journey log**
- Review 抓出 3 关键：订单生命周期每轮建新单（共同记忆断）、close/reduce 折叠成 hold（无法平仓）、风控被绕过。教训：**融合决策必须是动作级（含管理动作）而非方向级**；**风控只拦开仓、不拦减险出场**。
- 修复引入新问题：`_apply_risk` 无条件拦 close → 又不能平仓。**风险闸门要区分 entry/exit**——教训通用。
- `if conf and conf < min` 假穿风控（conf=0 跳过）；falsy 判断在风控里是陷阱。

## [S1] Problem

需要**多个策略人格共同管理订单**：
- **拓扑 A**：N 个人格共同管理**一个账户上的一笔真实订单/仓位**
- **拓扑 B**：N 个人格各自账户**同步开平**（多账户镜像）

当前每 bot 独立决策独立执行，无共识机制。需**共同订单上下文** + **决策融合**。

## [S2] Design

### [S2.1] 两种拓扑（配置可选）

```yaml
# config/persona_groups.yaml
groups:
  - name: btc-trio
    topology: single_account       # single_account | mirror_accounts
    members: [brooks-btc, smc-trader, scalper]
    target_account: brooks-btc     # single_account：订单落在谁
    fusion: weighted_vote          # weighted_vote | master_arbiter | consensus
    fusion_config:
      weights: {brooks-btc: 2, smc-trader: 1, scalper: 1}
    on_conflict: hold              # hold | master | majority
```

| 拓扑 | 订单落点 |
|---|---|
| `single_account` | N 人格 → 1 账户一单（去重单执行） |
| `mirror_accounts` | N 人格 → N 账户同步（原子写+校验） |

### [S2.2] 决策流程

```
触发 → 各人格独立分析（各自 prompt/工具/思考）→ 各自 Plan
     → 融合层（weighted_vote|master_arbiter|consensus）→ 1 个动作 → 按拓扑执行
```

### [S2.3] 融合（动作级，含管理动作）

| 模式 | 规则 |
|---|---|
| `weighted_vote` | 各决策类（long/short/hold/**close/reduce/modify**）权重过半 → 执行 |
| `master_arbiter` | master 的 Plan 定动作，其他作参考 |
| `consensus` | 共识分（confidence 加权）≥ threshold 才执行 |

- **决策类含管理动作**：close/reduce/modify 不折叠为 hold（否则无法平仓）
- **冲突**：long/short 冲突时 `on_conflict`：hold（默认）/ master / majority
- **action 字段**：融合输出 `action`（open_long/close/reduce_long/modify_tp_sl…）为执行权威

### [S2.4] 讨论模式（3 阶段，可选）

`discussion.enabled: true` 时，融合前插入辩论阶段：

| 轮次 | 阶段 | 行为 |
|------|------|------|
| 1 | 相互讨论与反驳 | 看同伴 decision+reasoning；可反驳、可被说服；不盲从多数 |
| 2 | 深化讨论 | 聚焦未解决分歧，追求更高置信度共识 |
| 3 | 最终决策 | 综合定稿，不再摇摆 |

- `rounds: 3`（硬上限 4）；`early_exit_on_agreement` 控制是否一致即退
- 每轮只交换 `decision + reasoning(≤50字)`，不暴露完整 Plan
- **改口可审计**：`data/shared/discussion_log.jsonl` 记录每轮谁从什么改成什么

### [S2.5] 稳定性（不掉票）

融合要求**每个成员都投票**；成员失败会削弱融合质量。四层保护：

| 机制 | 触发 | 行为 |
|------|------|------|
| LLM 重试 | 5xx / 网络中断 / 超时 | 指数退避 3 次；4xx 不重试 |
| 降级 hold | 重试仍失败 / 解析失败 | 返回 hold（`ok=True`），保留投票权 |
| 异常降级 | `analyze_once` 抛异常 | 同上，不掉票 |
| 账户缺失继续 | 取不到账户 | 警告后继续行情分析 |
| 强制查数据 | 全程无工具调用 | 明确要求后重试一次 |

**工具用量审计**：`tool_usage` + `tool_usage_summary` 随 `thinking.json` 落盘，防偷懒。

### [S2.6] 共同记忆（共享订单库）

`data/shared/orders/<order_id>.json`（跨 bot 读写）：

- **生命周期**：开仓建新单 → 持仓期**复用同一 order_id**（votes/reason/log 累积）→ 平仓标 closed
- 记录：订单状态 / 各人格理由 / 每轮投票 / 融合决策日志

### [S2.7] 执行映射

| 拓扑 | 执行 |
|---|---|
| `single_account` | 写 `target_account` inbox，**去重单执行** |
| `mirror_accounts` | 广播各成员 inbox（原子写 + JSON 校验 + exists 跳过） |

### [S2.8] 风控（不绕过、不误拦）

融合信号在写 inbox 前过 `source_bot` 的 strategist 风控：
- `min_confidence`：低于阈值 → 降级 hold（含 confidence=0）
- `max_notional_usd`：超限 → 封顶
- **减险出场豁免**：close/reduce/modify 不被风控拦（不能因低置信度而无法平仓）

### [S2.9] 进程

```bash
python -m omnialpha persona-run --group btc-trio   # 常驻
python -m omnialpha persona-run --once             # 单轮
```

## [S3] Out of Scope

- 人格间实时协商（只投票不聊天）
- 跨组联合调度、UI、热更新组成员

## Tasks

- [x] T1: persona_groups.yaml 配置加载与校验（covers: S2.1）
- [x] T2: 共享订单库 order_id 索引、votes/reason/log、跨 bot 读写（covers: S2.4）
- [x] T3: 各人格独立分析编排 analyze_once（covers: S2.2）
- [x] T4: 融合层三模式 + 动作级决策 + 冲突处理（covers: S2.3）
- [x] T5: 执行映射 single 去重 / mirror 同步（covers: S2.5）
- [x] T6: persona-run 进程 + 风控接入（covers: S2.6; S2.7）
- [x] T7: 测试 — 融合/生命周期/风控豁免/8 关键回归（covers: S2.3–S2.6）
- [x] T8: 文档 — README/AGENTS/HOWTO（covers: S2.7）
