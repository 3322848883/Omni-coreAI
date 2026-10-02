---
feature: strategy-score
status: delivered
updated: 2026-09-28
branch: feature/strategy-score
commits: 98d4fec..HEAD
---

# 策略评分板与试错台账（strategy-score）

## Report

**What was built** — 新增 `omnialpha/metrics/`：从各 paper `account.db` 只读聚合 equity/fills，计算收益、最大回撤、Sharpe/Sortino/Calmar、胜率/盈亏比/期望值、PSR/**DSR**（Bailey–López de Prado，阈值在 SE 单位）、OOS 30d Sharpe、成本拖累；`trials.py` 扫描 `prompts/*.md` + `config/bots/*.yaml` 内容哈希，变更即累计试错次数 N（共享 prompt 分摊到全部 owner bot）；`paper-score` CLI 输出排序表并写 `data/metrics/scoreboard.json`。排序：样本天数&lt;3 时 OOS 优先，否则 DSR 主序。风控按用户决策**不加统一硬闸**（策略自管）。

**Verification** — `unittest tests.test_metrics_stats tests.test_metrics_scoreboard` 14 PASS；`unittest discover -s tests` 463 OK（含 PRE-EXISTING 偶发 `test_shared_order_500_votes` 本轮未触发）；`python -m omnialpha --root <repo> paper-score` 对 24 个 paper yaml 出表（17 有账本 + 7 no_data）。

**Journey log**
- DSR 阈值必须用 `z = srs/se − E[max N]`（SE 单位）；当成年化 Sharpe 再 `/√365` 会严重低估试错惩罚（Review 抓出）。
- 共享 prompt（vergex×6、eth_range×3）试错 N 必须 fan-out 到所有 owner，否则 bots 间不可比。
- worktree 无 .venv，单测需指主仓 python；`--root` 必须放在子命令前。
- 审查驱动修复：Sortino 下行偏差、low_n、dsr 排序测试、OMNIALPHA_ROOT 解析均来自 review 清单。

## [S1] Problem

多套策略人格在本地模拟盘并行赛马（15–20 路，T0 统一 10k）。当前只有账户余额与 fills 原始数据，**没有**可信评分：

- 无法回答「谁真正强、谁是运气」——多策略并行等价于多次试错，裸 Sharpe/收益会系统性奖励噪声（Bailey–López de Prado MinBTL/DSR；见 `research/strategy-eval/`）。
- 试错次数 N 无处记账：改 prompt / 调 yaml 这类「又试了一次」不会被记录，DSR 无法校正。
- 原 `paper-audit` 仅体检查错，不产出可比较分数。

用户要求：做 **①评分板 + ②试错台账**；**③风控留给策略自管**（系统不新增统一切风格的硬闸，只保留既有护栏）。

## [S2] Design

### 2.1 组件

```text
omnialpha/metrics/
  __init__.py / __main__.py
  series.py      # equity/fills 序列读取（paper account.db 只读）
  stats.py       # 纯函数：return/maxdd/sharpe/sortino/calmar/pf/winrate/expectancy/skew/kurt/psr/dsr
  trials.py      # 试错台账：扫描 prompt/yaml 变更 → data/trials.jsonl，per-bot N
  scoreboard.py  # 聚合 → data/metrics/scoreboard.json + 控制台表
  cli.py         # python -m omnialpha paper-score | metrics.cli
```

### 2.2 指标口径（日频，pnl_snapshot.equity）

| 指标 | 定义 |
|------|------|
| return_pct | (equity_end/equity_start − 1)×100 |
| maxdd_pct | equity 峰谷最大回撤 % |
| sharpe / sortino | 日收益年化；sortino 下行半标准差 √mean(min(r,0)²) |
| calmar | 年化收益代理 / maxdd |
| win_rate | round-trip 胜率（%）；profit_factor / expectancy 同源 |
| trades_n | realized_pnl≠0 的平仓笔数（简化 round-trip） |
| psr | Probabilistic Sharpe（四阶矩） |
| **dsr** | Deflated Sharpe：`z = srs/se − E[max_N]`（SE 单位）；家族规模 = max(全局 trials, bot 数) |
| oos_30d_sharpe | 近 30 日子样本 Sharpe |
| low_n | snaps&lt;14 或收益序列&lt;14 标记 |

### 2.3 试错台账（trials）

- `data/trials.jsonl`：`{ts, bot_id, kind: prompt|config, path, sha256_8, note}`
- 扫描 prompts + config/bots 内容哈希；**共享 prompt 对每个 owner 各记一笔**；state 键 `bot:path`
- per-bot N = 该 bot 记录数；DSR 家族用 max(总 trial 数, bot 数)

### 2.4 评分板

- 列：bot, days, n_trades, N, return%, dd%, sharpe, dsr, oos30, calmar, pf, win%, grade
- 排序：days&lt;3 → oos30 优先；否则 **dsr** 主序、oos30 次序
- grade：explore / watch / cand（样本量徽章，不阻断交易）

### 2.5 错误行为
- 无 account.db → `no_data` 行，不抛垮全表
- 只读 db，不写 account.db

### 2.6 测试边界
- `tests/test_metrics_stats.py`：maxdd/pf/winrate/expectancy 数值、DSR=N1≡PSR、N=17 惩罚、Sortino 公式
- `tests/test_metrics_scoreboard.py`：双 bot dsr 排序、no_data、共享 prompt N fan-out、trials N+1/稳定

## [S3] Out of Scope

- **统一风控硬闸/熔断/热管理**——按用户决策交策略自管；系统侧仅保留 `max_notional_pct`、公式钳制、`max_notional_usd`
- 告警/webhook、晋级 L0–L4 自动执行、实盘对账、滑点/深度模型
- 改撮合引擎、改 executor 下单语义
- web UI

## Tasks
- [x] T1: `metrics/series.py` + `stats.py` 纯函数与 round-trip 配对 — acceptance: 单测数值正确 (covers: S2.2)
- [x] T2: `metrics/trials.py` 变更扫描与 N 计数 — acceptance: 改文件 N+1、共享 prompt fan-out、无变更 N 稳定 (covers: S2.3)
- [x] T3: `metrics/scoreboard.py` + CLI 表/JSON — acceptance: 双 bot 出表按 dsr 排序、no_data 不崩 (covers: S2.4–2.5; depends: T1, T2)
- [x] T4: 全量 unittest + 真实 scoreboard — acceptance: 463 不回归、输出含全部 paper bot 行 (covers: S2; depends: T3)
