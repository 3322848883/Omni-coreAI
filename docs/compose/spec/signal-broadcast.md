---
feature: signal-broadcast
status: delivered
updated: 2026-09-27
branch: feat/signal-broadcast
commits: 022d536..HEAD
---

# 信号广播（Signal Broadcast）

## Report

**What was built** — 一条信号同时在多个交易所账户执行的广播机制。`config/broadcast.yaml` 定义路由「源 inbox → 目标 bot 列表」，目标可为任意子集（1 个/2 个/N 个）。`python -m gate_bot broadcast` 常驻自动分发（`--once` 单轮）。目标列表**只来自系统配置**，AI 信号里的 `targets`/`exchanges` 字段一律忽略（防乱输入）。目标 bot 各自独立执行（密钥/风控/持仓/盈亏互不影响）。

**Verification** — `python -m unittest discover -s tests`：**308 PASS**（含 15 个 broadcast 测试 + 6 个关键修复回归）。端到端实测：1 信号 → 2 目标全收到；恶意 `targets` 注入被忽略、attacker-bot 未创建；源归档；审计日志留痕。

**Journey log**
- Review 抓出 5 个关键问题：同源多路由饥饿、失败被归档成 done、partial 退出码 0、bot_id 路径穿越、源 bot 未校验。教训：**「取件→处理→归档」循环要明确 consume-once 语义**；CLI 退出码必须把 partial 算失败。
- bot_id 净化应走 `paths.BotPaths`（唯一净化入口），自拼路径会漏防护。
- AI 注入防护要**显式忽略并记日志**，不是只靠「不读那个字段」。

## [S1] Problem

需要**一条信号同时在多个交易所账户执行**，且可配置投 1 个、2 个或多个目标。此前一个 bot 只绑定一个交易所，一份信号只在一个所执行。手动复制信号到多 inbox 易出错、无审计。

核心约束：
1. **可靠稳定**：投递必须逐个校验，失败要报错，不半途丢信号
2. **各交易所互不影响**：目标 bot 独立执行、独立风控/持仓/盈亏
3. **AI 不能乱输入**：投给谁由**系统配置**决定，AI 写的信号里**碰不到目标列表**

## [S2] Design

### 总体结构

```
AI / 手工信号 ──► data/bots/<from>/inbox/
                       │
              broadcast 进程（读 config/broadcast.yaml）
                       │ 按 route 复制到目标 inbox（逐个校验）
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   gate-btc/inbox  okx-btc/inbox  binance-btc/inbox
        │              │              │
   各自 executor（独立密钥/风控/持仓/盈亏）
```

**目标列表只来自 `config/broadcast.yaml`（系统配置），信号里任何 targets 字段一律忽略。**

### [S2.1] 配置（`config/broadcast.yaml`）

```yaml
routes:
  - name: multi-scalp
    from: signal-feed              # 源 bot_id
    to: [gate-btc, okx-btc]        # 目标 bot_id 列表（1/2/N 任意子集）
    enabled: true
```

- `from`/`to` 的 bot 名必须**存在**，否则拒绝启动（不静默跳过）
- bot_id 防路径穿越（拒 `/` `\` `..`），走 `paths.BotPaths` 净化
- 信号 JSON 中 `targets`/`exchanges` 字段**忽略**并在审计日志标注

### [S2.2] 广播进程（`gate_bot/broadcast.py` + `python -m gate_bot broadcast`）

- **常驻**（默认）/ `--once` / `--config <path>`
- **同源多路由**：按源分组，取件一次、整批分发（不饥饿）
- **逐个校验**：写入后确认文件存在且 JSON 可解析；任一失败记错误
- **原子落盘**：唯一临时名 + rename，防半读/并发碰撞
- **归档**：全成 → `archive/broadcast-done/`；任一失败 → `archive/broadcast-failed/`（可重投）
- **退出码**：`--once` 时 partial 算失败（非 0）
- **审计**：`data/broadcast/log.jsonl`（源、目标、成败、忽略的注入）
- **幂等**：同名已存在跳过

### [S2.3] 隔离保证

目标 bot 是既有 bot 机制，各自独立（密钥/风控/label_prefix/inbox/持仓/盈亏）。广播只做文件复制，一个目标失败不影响其他。

### [S2.4] 安全

- 目标列表只读配置，AI 无法增删目标
- 目标/源 bot 不存在 → 拒绝启动
- bot_id 防穿越
- 信号内 `targets`/`exchanges` 忽略并记日志

## [S3] Out of Scope

- 信号内容按目标差异化（同信号原样复制）
- 跨所原子下单、跨所总仓位聚合
- UI/管理界面、广播组嵌套、条件路由

## Tasks

- [x] T1: broadcast.yaml 配置加载与校验 — routes、任意子集、bot 存在、防穿越（covers: S2.1）
- [x] T2: 广播分发器 — 复制、原子写、逐个校验、失败报错、同源多路由（covers: S2.2）
- [x] T3: 广播进程 — `broadcast` 常驻 + `--once`、审计日志、退出码（covers: S2.2）
- [x] T4: 安全与隔离 — 忽略 targets、目标/源不存在拒绝、防穿越、失败归档（covers: S2.3; S2.4）
- [x] T5: 测试 — 路由/分发/失败/幂等/注入/5 关键回归（covers: S2.1–S2.4）
- [x] T6: 文档 — README/AGENTS/HOWTO（covers: S2.1）
