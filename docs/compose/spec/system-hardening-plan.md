# 系统深度归因与升级方案

> 基于 21 个模拟盘全面体检 + 实盘/纸面链路联调问题。  
> 状态：**P0.1–P0.3 / P1.1–P1.4 已落地**（见 §6）；P0.4（alerts.json）未做。

---

## 1. 问题归因矩阵

| 现象 | 根因 | 属性 | 现状 |
|------|------|------|------|
| 同一 `order_id` 成交入账 2 次 | 触发单「查→触发→下单」非原子；多线程/双进程竞态 | **系统** | **已修**：`claim_price_order` + `_apply_fill` 幂等 |
| 账户权益被抽穿（scalper −49 万） | 双倍入账 + 超大仓位资金费 | **系统为主** | 账本已重置；根因已修 |
| 单笔名义 5 万–15 万（本金仅 1 万） | 1% 公式 × 极窄止损 → 名义爆炸；`max_notional` 当「目标仓」用 | **策略 60% / 系统 40%** | 提示词已强调公式；**已修**：`account_risk.max_notional_pct`（默认 5×权益） |
| 平仓后遗留孤儿 SL/TP | `close`/强平/TP 成交后**不回收** reduce-only 条件单 | **系统** | **已修**：`executor._cleanup_orphan_protectors` |
| `hold`+tp/sl 被静默丢弃 | bridge 跳过 hold；schema 丢字段 | **系统** | **已修**（`modify_tp_sl`） |
| `bot_id` 写成 `inbox` | 布局 v2 路径名当 bot id | **系统** | **已修** |
| 模型把 `type` 写成动作名 | 枚举混淆 | 策略+系统 | **已修**（解析恢复 + 契约写清） |
| 1% 仓位不按公式、抄 max_notional | LLM 从众 | **策略** | 提示词已压；**已修**：`_align_size_to_risk` 程序验算 |
| 输出混 SMC 术语 / 缺区域声明 | 人格不纯 | **策略** | 多份提示词已禁/强制 |
| `interval_sec` 与 `poll_interval_sec` 易混 | 配置同名近义 | 系统（可用性） | 可改名/文档强调 |
| 双 paper-run 并发（pythonw 父子） | 启动脚本 + PID 锁竞态 | **系统** | **已修**：PidLock 改 OS 文件锁（msvcrt/flock），worker 持锁 |

---

## 2. 系统 vs 策略（结论）

```
系统缺陷（会让账算错、单重复、保护丢失）
  ├─ 撮合/入账并发非原子          → 已修（claim + 幂等 fill）
  ├─ 平仓后不清理孤儿 SL/TP       → 已修（_cleanup_orphan_protectors）
  ├─ 无「权益比例」级仓位硬顶     → 已修（max_notional_pct，默认 5×）
  ├─ paper 账本无跨进程文件锁     → 已修（_FileLock + msvcrt/flock）
  ├─ 入场后无持续风控（日亏/熔断）→ 已修（halt + daily_loss_limit_usd）
  └─ 可观测性弱（双倍成交无告警）→ 部分（notify/decay 有；alerts.json 未做）

策略问题（系统算对了，但单子质量差）
  ├─ 仓位公式执行不稳定          → 提示词 + 可程序验算 P1
  ├─ 人格混用术语 / 纪律漂移      → 提示词已改，持续盯输出
  └─ 剥头皮高频摩擦（fangfangtu） → 策略本身风险特征
```

**一句话：账目级灾难是系统问题；盈亏好坏主要是策略问题。**

---

## 3. 升级方案（按优先级）

### P0 — 正确性（必须）

| # | 项 | 做法 | 验收 |
|---|----|------|------|
| P0.1 | **孤儿保护单回收** | `close`/强平/仓位归零时：撤该合约 reduce-only 条件单（仅本 bot label 前缀） | 平仓后 `orphanSLTP=0` |
| P0.2 | **权益比例仓位硬顶** | executor：`size_usd ≤ equity × max_notional_pct`（**默认 5.0 = 5×权益**，即验收口径；非 50%）+ 绝对 max_notional | 1 万权益单笔名义 ≤ 5 万 |
| P0.3 | **入账唯一约束** | fills 表 `(order_id, size, price)` 防重；`_apply_fill` 再查一次 | 人工双写被拒 |
| P0.4 | **监控告警** | 权益偏离 &gt;10%、dup fill、orphan&gt;0 → 写 `state/alerts.json` | 体检脚本读告警 |

### P1 — 风控与稳定

| # | 项 | 做法 |
|---|----|------|
| P1.1 | **持续风控**：每 tick 检查日亏/权益下限，触线 `halt` 全新开仓 |
| P1.2 | **程序验算仓位**：按 `sl` 距离反推应有 size_usd，偏差 &gt;30% 时钳制并在 result 写 warning |
| P1.3 | **跨进程文件锁**：paper `account.db` 操作加 `portalocker`/`msvcrt` 文件锁 |
| P1.4 | **单实例强化**：plan-loop / paper-run 启动时二次校验 PID 文件，杜绝双跑 |
| P1.5 | **replace 收窄**：`replace: symbol` 默认只撤「过期入场」，不动有效 SL/TP（可配） |

### P2 — 产品与运维

| # | 项 | 做法 |
|---|----|------|
| P2.1 | 体检 CLI：`python -m gate_bot paper-audit` 定期跑 |
| P2.2 | 策略评分板：按 bot 输出 Sharpe/回撤/胜率（样本量标注） |
| P2.3 | 配置模板：`max_notional_pct` / `max_chips` / interval 命名统一 |
| P2.4 | 历史账本「可信度标记」：含 dup fill 的 bot 打 *，排名只比增量 |

---

## 4. 建议实施顺序（1–2 个工作会话）

```text
会话 A（今天）
  1. P0.1 孤儿回收 + 单测
  2. P0.2 权益比例硬顶 + 单测
  3. P0.3 fills 唯一 + 单测
  4. 全量 unittest

会话 B
  5. P1.1/1.2 持续风控 + 仓位验算
  6. P1.3/1.4 进程与库锁
  7. 重置被污染账本（fangfangtu 等）后对齐增量对比
```

---

## 5. 策略侧（非系统）同步建议

1. 所有人格统一口径：**size_usd 公式必须写**，否则 executor 钳制。  
2. 剥头皮类（fangfangtu/scalper）限制 `max_chips` 与日交易次数。  
3. 排行榜：引擎修复后 **T0 重置全部 paper 权益为 10000**，从同一时刻赛马。

---

## 6. 已完成（本阶段）

- 触发单原子占用 `claim_price_order`
- `_apply_fill` / `match_order` 幂等
- `modify_tp_sl` 全链路（Plan→bridge→schema→executor）
- `bot_id` 修复、Plan `type/action` 恢复
- AI 触发 `lookback` 上限 300 + `policy.limits` 生效
- **P0.1 孤儿保护单回收**：`executor._cleanup_orphan_protectors`（平仓/归零时撤本 bot reduce-only 条件单）
- **P0.2 权益比例硬顶**：`account_risk.max_notional_pct`（默认 5×权益）+ 绝对 `max_notional_usd`
- **P1.1 持续风控**：`account_risk.halt` + `daily_loss_limit_usd`（按 UTC 日初权益）
- **P1.2 程序验算仓位**：`_align_size_to_risk` 按 SL 距离反推并钳制
- **P1.3 paper 跨进程文件锁**：`paper/store._FileLock`（msvcrt/flock）
- **P1.4 单实例强化**：PidLock 改 OS 文件锁 + worker 自持锁（防 PID 复用/孤儿双开）
- paper 单测 24 + 全量 625（含锁 9 项）

### 未做

- **P0.4 监控告警 `state/alerts.json`**（权益偏离/dup fill/orphan 告警落盘）—— `notify.py`/`decay.py` 已有通道与衰减检测，但无 alerts.json 产物
