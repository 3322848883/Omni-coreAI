# OmniAlpha 系统级架构升级方案

> 2026-10-05 · 基于三份架构盘点（运行时/状态/风控可观测）+ 141 条外部调研 findings
> 配套证据：`REPORT.md`（外部实践）、`findings/F0-nofx.md`（本地参考项目）

---

## 一、现状架构（盘点结论）

### 1.1 运行时

8 种常驻进程，全部靠外部（systemd / 任务计划 / 脚本）保活，**无统一编排器**：

| 进程 | 职责 | 锁 | 谁守护它 |
|---|---|---|---|
| `plan-loop` | 单 bot LLM 策略循环 | `plan.lock` | supervisor / watchdog |
| `run` | 单 bot 信号执行循环 | `run.lock` | supervisor / watchdog |
| `paper-run` | 模拟盘执行 + 撮合 tick | `run.lock` | supervisor / watchdog |
| `persona-run` | 多人格共管融合 | `persona-<group>.lock` | **无人守护** |
| `supervisor` | 托管一组 bot 的子进程 | — | **无人守护** |
| `watchdog` | 全局补拉 + 飞书 | — | **无人守护** |
| `broadcast` | 信号扇出 | **无锁** | **无人守护** |
| `kline_watcher` | 行情采集（独立组件） | 自有 | systemd |

**一个 bot = `plan-loop` + `run`**，两者**只靠 inbox 文件目录通信**，无 RPC / 无队列 / **无回执通道**。

### 1.2 状态

**同一笔交易的状态至少有 9 份副本**，且**没有单一权威声明**：

| # | 副本 | 位置 | 性质 |
|---|---|---|---|
| 1 | 交易所 REST | 实时读 | **真实盘唯一权威** |
| 2 | executor 执行内快照 | 内存 | 单次 replace 内临时 |
| 3 | 共享订单库 | `data/shared/orders/*.json` | persona 的「意图记忆」，**非持仓权威** |
| 4 | 台账 SQLite | `data/bots.db` | 审计 |
| 5 | 成交日志 | `logs/trades.jsonl` | append-only 执行结果 |
| 6 | 记忆 journal | `state/memory_journal.jsonl` | 决策日志 |
| 7 | 记忆 profile | `state/memory_profile.json` | 统计画像 |
| 8 | paper account DB | `data/bots/<id>/paper/account.db` | **模拟盘唯一权威** |
| 9 | AI 快照 | snapshot 组装 | 给 LLM 的视图 |

**没有系统级对账**：唯一的 `reconcile_protection()` **只返回警告、不下单，且生产调用点为零**（唯一调用者是测试脚本）。真正在做一致性维护的只有「撤孤儿保护单」和「补裸仓 SL」两个**局部**扫描——它们只对账「保护单 vs 交易所持仓」，**不对账记忆/台账/shared orders vs 交易所**。

### 1.3 风控

5 层（账户级 / bot 级 / 单笔级 / 计划层 / 执行兜底），但存在两个致命边界问题：

- **`_stop_entry` 绕过账户级风控**：只调 `_check_symbol` + `_check_notional`，**不调** `_check_account_risk` / `_check_open_sl` → **突破单可以绕过 `halt` / `daily_loss_limit` / `max_leverage` / 总敞口闸门 / SL 必填**。
- **「账户级」风控实为逐 bot**：`account_risk` 逐 bot 配置，`_day_start_equity` 也按 bot 落盘。多 bot 共账户时，`max_total_notional` 读的是**全账户持仓**（口径正确）但阈值是**本 bot 的** → 各 bot 只卡各自阈值，**合计敞口 ≈ N × 阈值**，无合并闸门；日亏熔断同理，无法形成账户级熔断。

**告警与动作的脱节**：`equity_deviation` / `dup_fill` / `plan_fail` / decay **只落盘或推送**，不接自动动作（`equity_deviation` 的代码注释明写「不拦截，只落盘」）。接了动作的只有 watchdog 拉起、孤儿撤单、auto_protect 补 SL（且默认 `dry`）。

### 1.4 可观测性

- 每轮思考落 `thinking.json`：`reasoning_chain` 全量，但 **`content_head` 截 500 字**、工具结果截 20000 字
- 健康拆成 `health.json`（plan）+ `health.run.json`（run）两个文件，**无结构化指标导出**
- `omnialpha status` 只出 inbox/done/failed 计数，**不暴露健康**
- **讨论轮的 LLM 调用完全不落盘**（只有 30 字 reasoning 进 `discussion_log`）

---

## 二、结构性问题清单（23 条，按严重度分级）

### 🔴 严重（会造成实际损失）

| # | 问题 | 证据 |
|---|---|---|
| S1 | **突破单绕过账户级风控**（halt / 日亏 / 杠杆 / 总敞口 / SL 必填全部跳过） | `executor.py:1343-1361` vs `:817-819` |
| S2 | **账户级风控实为逐 bot** → N bot 共账户时敞口可叠加到 N× 阈值 | `executor.py:585-626` + `config.py:158` |
| S3 | **信号无执行层幂等**：重复投递 = 重复下单，无 dedup 键 | `watcher.py:97-176` |
| S4 | **无系统级对账**：本地记录与交易所漂移无检测 | `watcher.py:214`（只告警，零调用） |
| S5 | **告警不接自动动作**（Knight Capital 的 97 封邮件同型） | `alerts.py:22-25`、`executor.py:565-573` |
| S6 | **日初权益按 bot 落盘** → 多 bot 共账户无法形成账户级熔断 | `executor.py:632-657` |

### 🟡 中等（结构性弱点，会累积）

| # | 问题 | 证据 |
|---|---|---|
| S7 | 状态源不唯一（9 份副本，无权威声明） | 见 §1.2 |
| S8 | run 侧崩溃在「取件后、归档前」会**丢单且不重放** | `watcher.py:84-90` |
| S9 | AI 视图与执行视图**异步 + 容错语义不同**（snapshot 把取数失败当空列表，executor 严格区分） | `snapshot.py:61-100` vs `executor.py:2002-2012` |
| S10 | 审计三源（`trades.jsonl` / `bots.db` / `memory_journal`）**各写各的、互不校验** | `tradelog.py:30`、`ledger.py:90`、`journal.py:33` |
| S11 | 记忆副本**从不对交易所校验**（shared order 的 status 靠 persona 推断） | `runner.py:653-657,757` |
| S12 | `halt` 只能人工改 yaml，**无自动触发路径** | `executor.py:547` |
| S13 | 两套风控实现并存（`strategist/risk.py` vs `persona/runner.py`），判据易漂移 | 两文件 |
| S14 | 计划层无账户级预算（只告诉 AI 单笔预算，不含总敞口/日亏） | `loop.py:152-180` |
| S15 | profile 权威口径随环境切换（paper=账本 / live=文件累加） | `profile.py:74-79` |

### 🟢 工程卫生（不影响正确性，但拖累可维护性）

| # | 问题 | 证据 |
|---|---|---|
| S16 | **两个平行编排层**（supervisor 与 watchdog 都能拉起 plan/run） | `supervisor.py:80-88`、`watchdog.py:83-91` |
| S17 | watchdog 覆盖不全，`broadcast`/`persona-run`/`supervisor` 自身无人守护 | `watchdog.py:23-27` |
| S18 | **broadcast 无单实例锁** → 双开重复扇出 | `broadcast.py`（全文无 PidLock） |
| S19 | persona 组成员必须 `enabled:false` 是**隐式约束**（散在注释，非配置校验） | `watcher.py:414-428` |
| S20 | 可观测性截断（thinking 正文 500 字、工具结果 20000 字） | `loop.py:877,127` |
| S21 | 健康仅文件+日志，**无结构化指标导出**，`status` 不暴露健康 | `__main__.py:194-204` |
| S22 | 配置 overlay 对 list 只能整体替换，无法增量 | `config.py:102-105` |
| S23 | 无回执通道：persona 下单后靠异步扫 `trades.jsonl` 尾 500 行回连盈亏 | `runner.py:418-482` |

---

## 三、目标架构

对照外部实践（详见 `REPORT.md`），目标是把系统从「**多进程 + 文件通信 + 多份状态副本**」升级为「**分层 + 单一权威 + 对账驱动**」：

```svg
<svg viewBox="0 0 680 420" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif">
  <defs>
    <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
      <path d="M0,0 L10,5 L0,10" fill="none" stroke="#9ca3af" stroke-width="1.2"/>
    </marker>
  </defs>

  <text x="40" y="24" font-size="13" font-weight="500" fill="#111827">目标分层：意图 → 决策 → 执行 → 状态，每层有明确边界与唯一权威</text>

  <rect x="40" y="42" width="600" height="52" rx="8" fill="#eef2ff" stroke="#c7d2fe" stroke-width="0.5"/>
  <text x="56" y="62" font-size="12" font-weight="500" fill="#3730a3">① 意图层</text>
  <text x="56" y="80" font-size="11" fill="#4b5563">多个人格 / 多策略产出「意图」，不产出可执行指令（action + 方向 + 止损结构）</text>

  <rect x="40" y="106" width="600" height="52" rx="8" fill="#f5f3ff" stroke="#ddd6fe" stroke-width="0.5"/>
  <text x="56" y="126" font-size="12" font-weight="500" fill="#5b21b6">② 决策层（新增：账户级闸门）</text>
  <text x="56" y="144" font-size="11" fill="#4b5563">账户级合并风控（跨 bot 总敞口 / 日亏 / halt 状态机）→ 单笔风险预算 → 名义钳制</text>

  <rect x="40" y="170" width="600" height="52" rx="8" fill="#ecfdf5" stroke="#a7f3d0" stroke-width="0.5"/>
  <text x="56" y="190" font-size="12" font-weight="500" fill="#065f46">③ 执行层（幂等 + 回执）</text>
  <text x="56" y="208" font-size="11" fill="#4b5563">client_order_id 跨日唯一 · 执行结果写回回执 · SL 挂失败即回滚 · 保护单用 venue-computed close</text>

  <rect x="40" y="234" width="600" height="52" rx="8" fill="#fffbeb" stroke="#fde68a" stroke-width="0.5"/>
  <text x="56" y="254" font-size="12" font-weight="500" fill="#92400e">④ 状态层（唯一权威 + 对账）</text>
  <text x="56" y="272" font-size="11" fill="#4b5563">交易所 = 真实盘唯一权威 · 本地全部降为派生视图 · 周期对账 fail-closed</text>

  <rect x="40" y="298" width="600" height="52" rx="8" fill="#f3f4f6" stroke="#d1d5db" stroke-width="0.5"/>
  <text x="56" y="318" font-size="12" font-weight="500" fill="#374151">⑤ 编排层（收敛为一个）</text>
  <text x="56" y="336" font-size="11" fill="#4b5563">统一 supervisor 覆盖全部进程类型 · 显式约束校验 · 优雅停机</text>

  <rect x="40" y="362" width="600" height="46" rx="8" fill="#fef2f2" stroke="#fecaca" stroke-width="0.5"/>
  <text x="56" y="382" font-size="12" font-weight="500" fill="#991b1b">⑥ 可观测层</text>
  <text x="56" y="399" font-size="11" fill="#4b5563">完整留痕（prompt + CoT + 响应 + 执行）· 统一健康视图 · 告警接自动动作</text>
</svg>
```

**核心原则**（来自外部实践）：

1. **交易所是权威，本地是缓存**——显式对账 fail-closed，**缺报告 ≠ 空仓**（NautilusTrader）
2. **程序强制，提示词镜像**——硬边界在代码里，提示词只标注归属（nofx）
3. **能纠正就不拒绝**——钳制 vs 拒绝分层（nofx、Freqtrade）
4. **告警必须接自动动作**——否则不是控制（Knight Capital）
5. **幂等靠 venue identity，跨日唯一**（NautilusTrader、FIX ClOrdID）

---

## 四、专项：多实盘 bot 共用同一账户（S2 / S6 的具体化）

### 4.1 现状

`config/bots/` 里 `env: live` 的非样例配置共 **9 个**，**全部指向同一个 Gate 账户**（同一组 `GATE_API_KEY` / `GATE_API_SECRET`）：

| 配置 | label | 标的 | overlay | 当前状态 |
|---|---|---|---|---|
| brooks-btc | brk | BTC | `true` | 未跑（watchdog 停用） |
| chenmo-live | cm | BTC/ETH | 无 | 未启动 |
| scalper-live | sc | BTC/ETH | 无 | 未启动 |
| orderflow-live | ofl | BTC/ETH | `false` | 未启动 |
| smc-live | smc | ETH | `false` | 未启动 |
| wyckoff-live | wyk | BTC/ETH | `false` | 未启动 |
| smc-eth-live | sme | ETH | 无（手动 `--allow-disabled`） | **执行器在跑** |
| orderflow-eth-live | ofe | ETH | 无 | 讨论组成员 |
| wyckoff-eth-live | wye | ETH | 无 | 讨论组成员 |

**账户权益 85 USDT，50x 杠杆。** 各 bot 的单笔闸门：brooks-btc 5×权益、smc-live 5×、orderflow-live 5×、wyckoff-live 1×、eth-disc 三成员各 6×（单笔）/ 40×（总量）。

### 4.2 风险量化

**同时启动 N 个 bot 时，总敞口 ≈ Σ(各 bot 的 `max_notional_pct`) × 权益**，因为：

- `_check_account_risk` 读的是**全账户持仓**（口径正确），但**阈值是本 bot 自己的**（`executor.py:585-626`）
- `_day_start_equity` 按 bot 落盘（`executor.py:632-657`）→ 日亏熔断无法形成账户级

按当前配置粗算，若 6 个 live bot 全部启动，单笔闸门之和约 **27× 权益 ≈ 2,300 USDT 名义**（保证金 46 USDT，占权益 54%）——**这还是在没有任何一层合并闸门的情况下**，实际叠加取决于各 bot 的触发时机。

### 4.3 目标设计：账户级预算分配（三层）

**第 1 层 — 账户级总预算（新增，跨 bot 共享）**

引入一个**账户级配置文件**（按 `api_key_env` 分组，而不是按 bot），声明该账户的总量约束：

```yaml
# config/accounts.yaml（新增）
accounts:
  gate-main:
    api_key_env: GATE_API_KEY
    equity_budget_pct: 60.0        # 全部 bot 合计敞口 ≤ 权益 × 60%
    daily_loss_limit_usd: 8        # 账户级日亏熔断（不是每 bot 8）
    max_leverage: 50
    halt: false                    # 账户级 halt 状态机
```

所有 `api_key_env` 指向同一账户的 bot，其单笔闸门改为 `min(bot 自身阈值, 账户剩余预算)`。

**外部依据**：Freqtrade 的 `available_capital` 在多 bot 共享账户时给每个 bot 分配额度，并用 `tradable_balance_ratio`（默认 0.99）给手续费留余量 [F6-5]；nofx 的 `MaxMarginUsage` 是账户级而非 bot 级 [F0-4]。

**第 2 层 — 账户级 halt 状态机（新增，自动触发）**

`halt` 目前只能人工改 yaml（`executor.py:547`）。目标：由**账户级日亏 / 回撤 / 连续亏损**自动触发，并**自动恢复**（下一个交易日或人工解除）。

**外部依据**：Freqtrade 的四种 Protection 原语（连续亏损锁定 / 回撤停止 / 单币盈利性锁定 / 出场后冷却），每个可在不同阈值上实例化多次形成短/长期分层 [F6-1,2]；回撤必须按**权益曲线**算而非成交盈亏比之和 [F6-3]；日亏限制可以是「滑动窗口统计亏损止损次数 → 定时停机」，且**可以只作用于单边** [F6-4]。

**第 3 层 — 单笔闸门补全（修 S1）**

`_stop_entry` 补上 `_check_account_risk` + `_check_open_sl`。这是**当前最严重的单点缺陷**：突破单可以绕过 `halt` / 日亏 / 杠杆 / 总敞口 / SL 必填。

### 4.4 启动前检查清单（针对未启动的 8 个）

在任何未启动的 live 配置上线前，逐项确认：

| 检查项 | 为什么 |
|---|---|
| 该 bot 的 `api_key_env` 指向哪个账户 | 决定它和谁共享预算 |
| 该账户当前的**已分配预算**是多少 | 避免超出账户级总预算 |
| 该 bot 的 `symbols` 与其他已启动 bot 是否重叠 | 重叠标的的敞口会叠加（无跨 bot 去重） |
| 该 bot 的 `label_prefix` 是否唯一 | 订单命名空间冲突会导致误撤 |
| `position_policy` 与同标的其他 bot 是否冲突 | 一个 bot 的 close 可能平掉另一个 bot 的仓 |
| 是否已解决 S1（`_stop_entry` 绕过） | 否则该 bot 的突破单不受账户级约束 |

> **⚠️ 特别提示**：`smc-live`（`symbols: [ETH_USDT]`）与 eth-disc 三成员（同为 ETH）**标的完全重叠**。若同时启动，SMC 会以两种身份对同一标的下单，敞口与保护单都会互相干扰。

---

## 五、升级路线（按层，P0 → P2）

### P0 — 立即（安全缺口 + 正在发生）

| # | 改动 | 对应问题 | 依据 |
|---|---|---|---|
| **P0-1** | `_stop_entry` 补 `_check_account_risk` + `_check_open_sl` | S1 | 内部缺陷，无外部依据需求 |
| **P0-2** | 保护单改用 Gate 的 `auto_size=close_long\|close_short`（`size=0`），消除「张数对齐」不变量 | S4 局部 | Gate 官方字段文档 [F4-11]；交易所在持仓消失时自动撤 reduce-only [F4-10] |
| **P0-3** | 信号加**跨日唯一**的幂等键（`client_order_id`），执行层按它去重 | S3 | FIX ClOrdID 只要求单日唯一、建议嵌日期 [F4-13]；Hummingbot 按客户端订单 ID 对账 [F2-21] |
| **P0-4** | 告警接自动动作：至少让 `orphan_protector` / `equity_deviation` 触发具体处置 | S5 | Knight Capital 的 97 封邮件 [F6-11]；nofx 的安全模式 [F0-8] |
| **P0-5** | 账户级预算配置（§4.3 第 1 层）+ 账户级 halt 状态机（第 2 层） | S2 / S6 / S12 | Freqtrade `available_capital` [F6-5]；nofx `MaxMarginUsage` [F0-4] |

### P1 — 短期（结构性）

| # | 改动 | 对应问题 | 依据 |
|---|---|---|---|
| **P1-1** | **新增对账循环**（fail-closed）：启动时 + 周期，交易所持仓 vs 本地记录；**缺报告 ≠ 空仓**；对不上拒绝交易 | S4 / S7 | NautilusTrader reconciliation [F4-5,6,7]；nofx `position_reconcile` [F0-6] |
| **P1-2** | 明确**状态权威声明**：交易所（live）/ `account.db`（paper）为唯一权威，其余全部标注为「派生视图」 | S7 / S9 / S10 | — |
| **P1-3** | 执行层**回执通道**：执行结果写回，替代「异步扫 trades.jsonl 尾 500 行」 | S23 / S8 | Hummingbot ExecutorOrchestrator 持久化执行器数据 [F2-19] |
| **P1-4** | run 侧**幂等重放**：崩溃在「取件后、归档前」的单可恢复 | S8 | NautilusTrader 的 `submission_recovery_policy` [F4-8] |
| **P1-5** | 统一健康视图（含结构化指标导出）+ `status` 暴露健康 | S21 | nofx 的 `GetStatus` 结构化暴露 [F0-9] |
| **P1-6** | 决策**完整留痕**：prompt + CoT + 原始响应 + 执行日志；取消 500 字截断 | S20 | nofx `DecisionRecord` [F0-9] |

### P2 — 中期（工程卫生）

| # | 改动 | 对应问题 |
|---|---|---|
| P2-1 | 收敛编排层：统一 supervisor 覆盖全部进程类型（含 `persona-run` / `broadcast` / 自身） | S16 / S17 |
| P2-2 | `broadcast` 加单实例锁 | S18 |
| P2-3 | persona 组成员的 `enabled:false` 约束**配置化校验**（不再靠注释） | S19 |
| P2-4 | 统一两套风控实现（`strategist/risk.py` 与 `persona/runner.py`） | S13 |
| P2-5 | 计划层预算补账户级信息（总敞口 / 日亏剩余），不只单笔 | S14 |
| P2-6 | 配置 overlay 支持 list 增量合并 | S22 |
| P2-7 | 永续护栏：止损-强平缓冲、接近强平告警、进程死亡撤挂单 | — |
| P2-8 | 统计门槛：回测报告附显著性检验（t 检验 p-value） | — |

---

## 六、迁移顺序与验证

**建议顺序**（每步可独立验证、可回滚）：

1. **P0-1**（补闸门）——最小改动、最高收益，先做
2. **P0-2 实测**：确认 Gate `price_orders` 是否接受 `auto_size` / `close=true`。
   - 可行 → 保护单问题从语义上消失，P1-1 的复杂度下降
   - 不可行 → 退回「张数不变量 + 入场单幂等」方案
3. **P0-5**（账户级预算）——**这是「再启动任何一个 live bot 之前」的前置条件**
4. **P0-3 / P0-4**（幂等 + 告警接动作）
5. **P1-1**（对账循环）——系统级最大结构性补强
6. P1/P2 其余项按需

**每步的验证方式**：

| 改动 | 验证 |
|---|---|
| P0-1 | 构造一个 `stop_entry_long` 且 `halt: true` 的用例，断言被拒 |
| P0-2 | 真实账户挂一张 `auto_size=close_long` 的保护单，平掉部分仓位后确认它仍有效 |
| P0-3 | 同一信号文件投递两次，断言只下单一次 |
| P0-4 | 注入一个孤儿保护单，确认自动处置而非只落盘 |
| P0-5 | 两个 bot 同时请求接近各自上限的名义，断言合计不超账户预算 |
| P1-1 | 手工在交易所开一个本地无记录的仓，断言对账循环报出并拒绝交易 |


---

## 四、升级路线

按「收益 ÷ 成本 × 风险」排序。**每一批都可独立交付、独立验证**，不需要一次性重构。

### 批次 1 — 堵住风控绕过（最高优先，改动最小）

**1.1 `_stop_entry` 补齐账户级闸门**（S1）

- **改法**：`executor._stop_entry` 开头补 `_check_open_sl(intent)` 与 `_check_account_risk(intent)`，与 `_open` 完全同源。
- **依据**：nofx 的 `validateProtectionPrices` 在两条入场路径上都校验；SEC 15c3-5 要求控制是「盘前 + 自动 + 覆盖全部下单路径」。
- **风险**：极低。但**要注意**：补上 `_check_account_risk` 后，之前能过的突破单可能被拒——需先用历史信号回放确认拒单率不会异常升高。
- **验证**：写测试断言「`stop_entry_*` 与 `open_*` 在同样超限输入下**被同一条闸门拒绝**」。

**1.2 新增账户级风控层**（S2、S6、S12、S14）

- **改法**：新增 `config/accounts.yaml`（账户级配置，与 bot 解耦），声明：
  ```yaml
  accounts:
    gate-main:
      api_key_env: GATE_API_KEY
      max_total_notional_pct: 10.0      # 账户级总敞口上限（跨全部 bot）
      daily_loss_limit_usd: 20          # 账户级日亏熔断
      halt: false                        # 可由程序自动置位
      bots: [smc-eth-live, orderflow-eth-live, ...]
  ```
  executor 构造时接收**账户级配置 + bot 级配置**，闸门分两级：bot 级先过、账户级再过（账户级用**该账户全部 bot 的合计敞口**）。
- **依据**：nofx 的 `available_capital`（多 bot 共享账户时分配额度）；Freqtrade 的 `tradable_balance_ratio` + 每 bot 资本分配；SEC 15c3-5 的「总量资金阈值必须能真正拦截订单」（Knight Capital 的缺陷正是这个）。
- **风险**：中。需要梳理现有 `account_risk` 的字段归属（哪些上移到账户级、哪些留在 bot 级）。**建议保留 bot 级字段作为兼容，账户级为新增的「合并闸门」**。
- **验证**：起两个 bot 共账户，各自开到 bot 上限，断言**账户级闸门在合计超限时拒绝第三个**。

**1.3 `halt` 加自动触发路径**（S12）

- **改法**：`halt` 不再只由 yaml 人工设置，而是**由日亏熔断自动置位**（写回账户级状态文件），并在下一交易日自动复位；人工 `halt: true` 仍然有效（逻辑或）。
- **依据**：Freqtrade 的 Protection 对象（`MaxDrawdown` / `StoplossGuard` 触发后**定时锁定**，到期自动解锁）；nofx 的 `safe_mode`（连续失败自动进入、恢复后自动退出）。
- **风险**：低。关键是**复位条件要明确**（按日 / 按时长），避免永久停机无人知。

### 批次 2 — 状态一致性与对账（系统级最核心）

**2.1 显式声明权威源，其余降为派生视图**（S7）

- **改法**：写一份 `docs/architecture/state-authority.md`，逐条声明每份状态副本的**权威性等级**：
  | 副本 | 等级 | 说明 |
  |---|---|---|
  | 交易所 REST | **权威（真实盘）** | 一切以它为准 |
  | paper account.db | **权威（模拟盘）** | 模拟盘唯一真值 |
  | 其余 7 份 | **派生** | 只读缓存 / 审计 / 视图，**不得反向覆盖权威** |
  并在代码里给派生视图加显式标注（如 `_derived: true`），禁止任何「用本地记录去修正交易所」的代码路径。
- **依据**：NautilusTrader「显式持仓报告才是权威，缺报告 ≠ 空仓」；本地调研（agent-memory-arch）的「仓位是派生查询，不是存储猜测」。

**2.2 新增对账循环**（S4）

- **改法**：新增 `omnialpha/reconcile.py`，提供 `reconcile_account(account_id)`：
  1. 拉交易所持仓 + 挂单 + 条件单
  2. 与本地派生视图（shared orders / 台账 / 记忆）比对
  3. **不一致时以交易所为准修正派生视图**，并落盘差异报告
  4. **fail-closed**：无法取得交易所报告时，**标记为 `unknown` 而非 `flat`**，且**阻断开仓**（不是阻断平仓）
- **挂载时机**：① 进程启动时；② 每 N 分钟（与 `orphan_sweep` 同节拍或独立）；③ **每笔开仓前**（轻量版：只校验 symbol 级）
- **依据**：NautilusTrader 的 reconciliation（启动时对齐否则拒绝启动、运行期用「近期活动宽限窗 + 单笔定向查询」、模糊在途命令故意保持未决）；nofx 的 `store/position_reconcile.go`（本地 OPEN 必须是交易所实盘的子集，不虚构 PnL）。
- **风险**：中。**核心风险是「修正方向搞反」**——必须只允许「交易所 → 本地」单向修正。
- **验证**：造一个「本地记录与交易所不一致」的场景（手工改 shared order），断言对账把它拉回，且**不产生任何下单动作**。

**2.3 信号加执行层幂等键**（S3、S8）

- **改法**：
  - 信号文件写入时带 `client_order_id`（**跨日唯一**：`{date}-{bot}-{cycle}-{seq}`），符合 Gate 的 `text` 约束（`t-` 前缀 + ≤28 字节 + 字符集）
  - executor 执行前查「该 `client_order_id` 是否已执行过」（查台账或本地执行记录），**已执行则跳过并归档为 duplicate**
  - 取件改为「**先记录意图，再执行**」：`.taking` 阶段就把 `client_order_id` 写入本地 pending 表，崩溃后重启能发现「取件了但没归档」的悬空项
- **依据**：NautilusTrader 的幂等靠 venue `trade_id` + 对账生成的成交用确定性 ID 以便重启重放去重；FIX `ClOrdID` 要求跨日唯一；Hummingbot 用**客户端生成的订单 ID** 对账。
- **风险**：中。注意 Gate 的 `text` 长度与字符集约束。

### 批次 3 — 执行层不变量

**3.1 SL 挂失败即回滚**（配合 REPORT 的 P1-2）

- **改法**：`_open` / `_stop_entry` 挂保护单失败时，若入场单**未成交**则撤单；**已成交则只告警不平仓**（沿用现有 `_rollback_unprotected_entry` 的约定，不要改成自动市价平仓）。
- **依据**：nofx 的 `closeUnprotectedPosition` + `emergencyClosePositionAndVerify`（3 次重试 → `CancelAllOrders` → `GetOpenOrders` 复查）；NautilusTrader 的「不假设清理成功」。

**3.2 保护单改用 venue-computed close**

- **改法**：挂 TP/SL 时用 Gate 的 `close=true, size=0` / `auto_size=close_long|close_short`，让保护单**在语义上是「平掉全部」**，彻底消除「保护单张数 vs 持仓张数」这个不变量。
- **依据**：Gate.io `FuturesOrder` 文档（`auto_size`）；NautilusTrader 把这类单白名单化并跳过数量检查。
- **风险**：**必须先实测** `price_orders` 接口是否接受这些字段（`auto_size` 在 `FuturesOrder` 上，`price_orders` 的嵌套结构需验证）。
- **若不可行**：退回到「张数不变量」兜底（见 REPORT 的 P0-2）。

**3.3 回执通道**（S23）

- **改法**：`run` 执行完成后，把结果写回一个**约定的回执位置**（如 `data/shared/receipts/<client_order_id>.json`），persona / plan 侧直接读回执，而不是扫 `trades.jsonl` 尾 500 行。
- **依据**：Hummingbot 的 `ExecutorOrchestrator` 用显式动作对象管理生命周期并持久化结果；nofx 的 `DecisionRecord` 保存执行日志。

### 批次 4 — 编排收敛

**4.1 合并 supervisor 与 watchdog 的职责**（S16、S17）

- **改法**：明确二选一——要么 watchdog 成为唯一编排层（supervisor 降为它的子命令），要么 supervisor 覆盖全部进程类型、watchdog 只做健康检查。**不要让两个层都能拉起同一个进程**。
- **依据**：nofx 的单一 AutoTrader 循环 + `isRunning` 检查点；Freqtrade 的单一 `worker`。

**4.2 补齐守护覆盖 + broadcast 加锁**（S17、S18）

- **改法**：watchdog 的「应有组件」清单覆盖 `broadcast` / `persona-run` / `supervisor` 自身；`broadcast` 加 `PidLock`（与 `run` 同款）。

**4.3 隐式约束显式化**（S19）

- **改法**：把「persona 组成员必须 `enabled: false`」变成**配置校验**——加载 persona group 时检查成员 bot 的 `enabled`，为 `true` 则报错并给出原因（否则会与 watchdog 拉起的 plan-loop 抢同一账户）。
- **依据**：nofx 的提示词分节标注「CODE ENFORCED」（程序强制的约束要显式）；以及「凡是 AI 可见的约束必须与系统实际执行的约束一致」。

### 批次 5 — 可观测性

**5.1 统一健康视图 + 结构化指标**（S21）

- **改法**：把 `health.json` / `health.run.json` / `alerts.json` / `bots.db` heartbeat 合并为**单一查询接口**（`omnialpha status --json` 或新增 `omnialpha health`），并导出结构化指标（进程存活、error_streak、llm_latency、权益、敞口、告警计数）。
- **依据**：nofx 的 `GetStatus` 输出 `safe_mode` / `ai_wallet_status`，面板直接显示而非翻日志。

**5.2 完整留痕（去掉截断）**（S20）

- **改法**：`thinking.json` 的 `content_head` 不再截 500 字（或另存完整版）；**讨论轮的 LLM 调用落盘**（现在完全不落）。
- **依据**：nofx 的 `DecisionRecord`（system prompt + user prompt + CoT + 原始响应 + 逐条执行日志）；本地调研的「LLM 理由文本不是审计证据，需要 grounded 工具调用 + 时间戳 + 执行日志」。

**5.3 告警接自动动作**（S5）

- **改法**：给每类告警定义**默认动作**（不是全部，按风险）：
  | 告警 | 建议动作 |
  |---|---|
  | `equity_deviation` | 超过阈值 → **自动降级为只平不开**（而非只落盘） |
  | `dup_fill` | 自动撤重复单 + 告警 |
  | `plan_fail` 连续 N 次 | 进入安全模式（只平不开，自动恢复） |
  | `orphan_protector` | 已有自动撤单 ✓ |
  | `unprotected_position` | 自动补 SL（`auto_protect` 从 `dry` 转 `true`） |
- **依据**：Knight Capital 的 97 封错误邮件；nofx 的 `consecutiveAIFailures>=3` → 安全模式；SEC 15c3-5 的「控制必须自动」。

---

## 五、迁移顺序与验证

```svg
<svg viewBox="0 0 680 240" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif">
  <defs>
    <marker id="a2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
      <path d="M0,0 L10,5 L0,10" fill="none" stroke="#9ca3af" stroke-width="1.2"/>
    </marker>
  </defs>

  <rect x="40" y="40" width="120" height="56" rx="8" fill="#fef2f2" stroke="#fecaca" stroke-width="0.5"/>
  <text x="100" y="62" font-size="12" font-weight="500" text-anchor="middle" fill="#991b1b">批次 1</text>
  <text x="100" y="80" font-size="10.5" text-anchor="middle" fill="#6b7280">堵风控绕过</text>

  <rect x="186" y="40" width="120" height="56" rx="8" fill="#fffbeb" stroke="#fde68a" stroke-width="0.5"/>
  <text x="246" y="62" font-size="12" font-weight="500" text-anchor="middle" fill="#92400e">批次 2</text>
  <text x="246" y="80" font-size="10.5" text-anchor="middle" fill="#6b7280">权威源 + 对账</text>

  <rect x="332" y="40" width="120" height="56" rx="8" fill="#ecfdf5" stroke="#a7f3d0" stroke-width="0.5"/>
  <text x="392" y="62" font-size="12" font-weight="500" text-anchor="middle" fill="#065f46">批次 3</text>
  <text x="392" y="80" font-size="10.5" text-anchor="middle" fill="#6b7280">执行不变量</text>

  <rect x="478" y="40" width="120" height="56" rx="8" fill="#f3f4f6" stroke="#d1d5db" stroke-width="0.5"/>
  <text x="538" y="62" font-size="12" font-weight="500" text-anchor="middle" fill="#374151">批次 4</text>
  <text x="538" y="80" font-size="10.5" text-anchor="middle" fill="#6b7280">编排收敛</text>

  <path d="M160 68 L182 68" fill="none" stroke="#9ca3af" stroke-width="1" marker-end="url(#a2)"/>
  <path d="M306 68 L328 68" fill="none" stroke="#9ca3af" stroke-width="1" marker-end="url(#a2)"/>
  <path d="M452 68 L474 68" fill="none" stroke="#9ca3af" stroke-width="1" marker-end="url(#a2)"/>

  <rect x="40" y="130" width="558" height="88" rx="8" fill="#f9fafb" stroke="#e5e7eb" stroke-width="0.5"/>
  <text x="56" y="152" font-size="12" font-weight="500" fill="#374151">每批次的通用验证要求</text>
  <text x="56" y="172" font-size="11" fill="#4b5563">① 全量单元测试通过（当前 1592 项）　② 新增行为有独立测试（含边界与反例）</text>
  <text x="56" y="190" font-size="11" fill="#4b5563">③ 用真实历史信号回放，确认拒单率/行为变化在预期内　④ 实盘先小额验证再全量</text>
  <text x="56" y="208" font-size="11" fill="#4b5563">⑤ 每批次可独立回滚（配置开关或代码 revert）</text>
</svg>
```

**建议执行顺序的理由**：

- **批次 1 先做**——它是纯收益（堵住绕过），且**不依赖其他改动**。1.1 尤其小（几行），可以立刻做。
- **批次 2 是系统级核心**——它把「多份状态副本」这个问题从架构上解决，之后批次 3/5 都会受益。**但它的风险也最高**（对账修正方向搞反会主动破坏正确状态），所以必须配套「单向修正 + fail-closed」的设计与充分测试。
- **批次 3 依赖批次 2**——回执通道与幂等键都需要「权威源明确」这个前提。
- **批次 4/5 可以并行做**，不影响交易正确性。

---

## 六、明确不做的事（边界）

1. **不引入消息队列 / RPC 框架**。当前规模（数十 bot、单人维护）下，inbox 文件 + 原子重命名是够用的；引入 broker 会带来新的运维面。**幂等键与回执通道可以在文件层实现**。
2. **不做微服务拆分**。保持单进程多 bot 的模型。
3. **不重写记忆系统**。它的四层结构（Order/Journal/Profile/Working）已有设计文档与实测，只需补「与交易所对账」这一环（批次 2.2）。
4. **不改 `sync`/`relay` 讨论模式的默认值**。辩论的实证结论（轮数是错的旋钮、sycophancy 是头号失败模式）建议做 A/B 而不是直接切换默认。
5. **不追求「企业级」对账频率**。NautilusTrader 的对账是毫秒级事件驱动的；我们的规模用「启动时 + 每 N 分钟 + 开仓前轻量校验」即可。

---

## 七、与既有调研的关系

| 文档 | 内容 | 关系 |
|---|---|---|
| `REPORT.md` | 外部实践调研（141 findings / 65 来源） | 本方案的**依据来源** |
| `findings/F0-nofx.md` | 本地参考项目 nofx 的设计优点 | 本方案的**对照基线** |
| `research/agent-memory-arch/REPORT.md` | 记忆架构调研（2026-09-28） | 「仓位是派生查询」「事件溯源」的**既有结论**，与本方案批次 2 一致 |
| **本文件** | 系统级升级方案 | 把上述依据落到 omnialpha 的**具体架构改动** |

---

*2026-10-05 · 现状盘点证据来自三份子代理报告（运行时/状态/风控可观测），每条均带 file:line 可复验*

