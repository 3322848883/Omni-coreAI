---
feature: runtime-upgrade
status: delivered
updated: 2026-10-20
branch: feat/runtime-upgrade
commits: dccce59..HEAD
---

# 长期运行升级：存储台账 · 按 bot 目录 · 统一 Supervisor

## Report

**What was built** — `data/bots/<id>/{inbox,archive,logs,state}` 新布局 + `data/bots.db` SQLite 台账（trades/plans/signals/heartbeats）+ `python -m gate_bot supervisor` 多 bot 托管（PID 锁、限频重启）+ `migrate` 一键迁移。执行/Plan 路径已接到 v2 布局；CLI 与 supervisor 锁一致（`GATE_LOCK_HELD`）。

**Verification** —
- `unittest discover -s tests` → **161 OK**（含 migrate 幂等、v2 paths、锁互斥）
- 两轮 review：4 critical 全关闭（布局接线 / 迁移重复导入 / 锁路径冲突 / supervisor 失败重试）

**Journey log** —
1. 首版只建了 paths/ledger/migrate，未改 `ProjectPaths` 运行时 → review 抓出「迁移后 run 不读新 inbox」。
2. migrate 重复导入 jsonl → `.jsonl_imported` 标记一次。
3. supervisor 与 CLI 抢同一 lock 文件导致子进程秒退 → `GATE_LOCK_HELD` + 统一 `lock_run` 路径。
4. PidLock 改 `O_EXCL` 原子创建，防 TOCTOU 双开。

## [S1] Problem

多机器人要 7×24 稳定、独立运行，当前有三块缺口：

1. **存储分裂**：行情已是 `kline.db`，但 bot 的 trades/plans/signals/心跳全是散落文件（`logs/`、`history/`、`archive/`），难检索、难报表、难探活。
2. **目录按「类型」而非按「bot」**：inbox/logs/archive 横切，多 bot 运维要跳多个根；命名、轮转、gitignore 不统一。
3. **进程托管缺口**：`plan-loop`/`run` 需成对常驻；无统一 supervisor、无单实例锁，易双开重复下单；崩溃拉起靠外置任务计划，与 `pa-data-source/watchdog.py` 运维不一致。

已定方向（用户确认）：**混合存储（文件投递 + SQLite 台账）** + **按 bot 重划目录树** + **统一 supervisor + 单实例锁** + **自动迁移**。

## [S2] Design

### 2.1 总览

```text
                    ┌──────────────────── supervisor ───────────────────┐
                    │  加载 config/bots/*.yaml                            │
                    │  每 bot 起 plan-loop + run（PID 锁、限频重启）        │
                    │  写心跳 → data/bots.db  heartbeats                  │
                    └────────────────────────────────────────────────────┘
                                      │
     data/bots/<bot_id>/  ────────────┼──────────── data/bots.db（台账）
       inbox/     信号投递（文件，人/AI/plan-loop）
       outbox/    plan-loop 写出待执行（可与 inbox 合一，见 2.3）
       archive/   done/ + failed/
       logs/      plan-*.log  run-*.log  trades.jsonl
       state/     last_cycle.json  ai_triggers.json  hold/*.json  lock

     pa-data-source/data/kline.db …  （只读行情，不变）
```

### 2.2 存储：混合（文件投递 + SQLite 台账）

**保留文件**（投递与审计原件）：
- `inbox/<id>/*.json` 信号意图（幂等源）
- `archive/**` 成败原件 + `*.result.json` / `*.error.json`

**新增 SQLite** `data/bots.db`（与 pa 的 kline 同技术栈，WAL）：

| 表 | 字段（要点） | 用途 |
|----|--------------|------|
| `trades` | ts, bot_id, plan_cycle, action, symbol, size_usd, price, order_ids, ok, steps_json | 执行流水（自 trades.jsonl 双写/迁入） |
| `plans` | ts, bot_id, cycle_id, trigger, orders, notes, reasoning, raw_json | LLM Plan 摘要 |
| `signals` | ts, bot_id, path, status(pending\|done\|failed), error | inbox 生命周期 |
| `heartbeats` | bot_id, component(plan\|run), ts, pid, detail | supervisor 探活 |

约定：
- **写路径**：执行器/plan-loop 写库为**主**台账；jsonl 仍可写作 append 审计（可配置 `ledger: sqlite|jsonl|both`）。
- **读路径**：`trades`/`plans` 查询 CLI（后续 `gate_bot report`），不阻断交易链路。
- **迁移**：一次性把现有 `logs/trades/*.jsonl` 与 `history/**` 导入 `bots.db`（见 2.5）。

### 2.3 按 bot 目录树

```text
data/bots/<bot_id>/
  inbox/          # 外部 + plan-loop 写入的信号 JSON
  archive/done/
  archive/failed/
  logs/           # stdout/err、trades.jsonl（若 ledger=both）
  state/          # last_cycle、ai_triggers、hold 审计、*.lock
```

规则：
- **一 bot 一树**；bot 之间路径零交叉。
- 配置仍在 `config/bots/<bot_id>.yaml`（密钥仅环境变量）。
- `label_prefix` / `order_scope` 隔离下单归属不变。
- 旧路径 `inbox/<id>`、`logs/trades/<id>.jsonl`、`history/<id>` **自动迁移**（2.5）。
- gitignore：`data/bots/*/inbox`、`archive`、`logs`、`state` 可提交空目录骨架可选。

### 2.4 统一 Supervisor（稳定运行）

新入口：`python -m gate_bot supervisor`（或 `gate-bot-supervisor`）。

职责（借鉴 `pa-data-source/watchdog.py`）：
1. 启动时 `migrate`（若检测旧布局）→ 扫描 `config/bots/*.yaml` 中 `enabled: true`。
2. 每 bot 两个子进程：`plan-loop`、`run`（可用配置关掉一侧）。
3. **单实例锁**：`data/bots/<id>/state/plan.lock`、`run.lock`（PID 存活检测；孤儿锁接管）。
4. **崩溃拉起**：退出后约 5s 重启；**≤5 次/小时**/组件，超限记 fault 并停该组件（防重启风暴）。
5. **心跳**：子进程或 supervisor 写 `heartbeats` 表。
6. **status**：`gate_bot status` 扩展为：每 bot 进程/PID/最近 plan_cycle/inbox 深度/心跳年龄。
7. **日志**：`data/bots/<id>/logs/`，建议按日切割或大小上限。
8. **独立性**：bot A 崩溃不影响 bot B；supervisor 本身可由 systemd/任务计划托管**单实例**。

配置钩子（yaml，可选）：
```yaml
runtime:
  plan_loop: true
  run: true
  restart_per_hour: 5
  heartbeat_sec: 30
  ledger: both    # sqlite | jsonl | both
```

### 2.5 自动迁移

`python -m gate_bot migrate [--bot id] [--dry-run]`：
1. 创建 `data/bots/<id>/{inbox,archive,logs,state}`。
2. 移动/复制：`inbox/<id>/*` → `data/bots/<id>/inbox`；`archive/{done,failed}/<id>` → 对应树；`history/<id>/*` → `state/`；`logs/trades/<id>.jsonl` → `logs/trades.jsonl`。
3. 导入 jsonl → `bots.db.trades`。
4. 写 `state/.layout_version = 2`；supervisor 启动时若见旧根且未迁移 → 提示跑 migrate（或 `--auto-migrate`）。

### 2.6 模块边界

| 模块 | 职责 |
|------|------|
| `gate_bot/paths.py` | 统一解析 bot 目录（新布局；兼容只读旧路径直到迁移） |
| `gate_bot/ledger.py` | bots.db 读写（trades/plans/signals/heartbeats） |
| `gate_bot/supervisor.py` | 进程托管、锁、限频重启 |
| `gate_bot/migrate.py` | 目录 + jsonl 迁移 |
| `gate_bot/__main__.py` | `supervisor` / `migrate` 子命令；`status` 扩展 |

### 2.7 测试边界

- 单测：paths 布局、migrate 幂等、ledger CRUD、supervisor 锁/限频（mock 子进程）。
- 集成：双 bot 启停、杀 plan-loop 后拉起、双开拒绝、migrate 后 archive/inbox 可读。
- 不测：真实 LLM 长跑、实盘下单回归（已有 prelaunch）。

## [S3] Out of Scope

- 多机/分布式部署、消息队列（Kafka 等）
- 替换 pa-data-source 采集与 kline.db
- 完整 Web 控制台 / HTTP 健康端点（可后续加）
- 加密鉴权的 inbox 投递
- 策略逻辑、提示词内容变更

## Tasks

- [x] T1: `paths.py` 新布局解析 + 默认骨架创建 — acceptance: 单测覆盖 data/bots/&lt;id&gt;/* 解析 (covers: S2.3)
- [x] T2: `ledger.py` + bots.db schema（trades/plans/signals/heartbeats） — acceptance: 单测 CRUD + WAL (covers: S2.2)
- [x] T3: 执行器/plan-loop 写入 ledger（可配置 jsonl/sqlite/both） — acceptance: 执行一笔后 bots.db 有 trades/plans 行 (covers: S2.2)
- [x] T4: `migrate.py` 目录+jsonl 迁移（幂等、--dry-run） — acceptance: 旧树样例迁移后结构正确 (covers: S2.5)
- [x] T5: `supervisor.py` 多 bot 托管 + PID 锁 + 限频重启 + 心跳 — acceptance: 双 bot 假子进程被拉起/限停 (covers: S2.4)
- [x] T6: `status` 汇总进程/心跳/inbox；文档 OPERATIONS 更新 — acceptance: status 显示多 bot 运行态 (covers: S2.4; depends: T5)
- [x] T7: 回归 unittest + 双 bot testnet 冒烟 — acceptance: 全绿，无双开重复下单 (covers: S2.7; depends: T3,T5)
