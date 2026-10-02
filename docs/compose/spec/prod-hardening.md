---
feature: prod-hardening
status: delivered
updated: 2026-09-29
branch: feat/prod-hardening
commits:
---

# 生产加固（回测+衰减检测+订单状态机+监控告警+波动率仓位）

## Report

**What was built** — 5 项生产加固：DecayDetector（滚动 Sharpe/胜率衰减告警）、HealthMonitor（延迟/错误/心跳告警）、订单状态机补全（mmp_canceled/IOC 部分成交）、vol_adjust_size（ATR 目标波动缩放仓位）、BacktestReplayer（journal 回放 + DSR/p-value/B&H/SMA 基准）。

**Verification** — python -m unittest discover -s tests → **589 OK**（含 27 项 prod-hardening 测试）。

**Journey log** —
1. 全同值收益序列导致方差为 0 → Sharpe/p-value 边界处理
2. historical Sharpe 为负时告警条件跳过 → 加负值转正触发
3. SMA 交叉测试需价格变向才有交易
4. 订单状态机对齐 OKX v5（mmp_canceled 终态）

## [S1] Problem

缺口分析（`research/trading-system-gaps/DECISION-REPORT.md`）确认 5 项生产必需功能缺失。当前系统 562 测试全过、实盘 brooks-btc 已跑通记忆，但缺：
1. **回测验证**：journal 有每轮决策+快照，但无法回答「策略是否真有 alpha」
2. **衰减检测**：7×24 裸奔，滚动 Sharpe 下降 50% 无人知
3. **订单状态机**：`mmp_canceled`/IOC 部分成交未覆盖，仓位记录与实际不一致
4. **监控告警**：LLM 超时/行情源断联是 silent failure
5. **波动率仓位**：BTC 波动 3% 和 1% 时 size_usd 不变，波动大时风险暴增

## [S2] Design

### [S2.1] 策略衰减检测（decay）

**数据源**：`memory_journal.jsonl`（已有 `decision`/`executed`/`exec_result`）。

**新增文件**：`omnialpha/monitoring/decay.py`

```python
class DecayDetector:
    """滚动窗口策略衰减检测。"""
    def __init__(self, root: Path, bot_id: str, window: int = 20):
        ...
    def record_cycle(self, cycle_id: str, decision: str,
                     executed: bool, pnl_usd: float = 0.0) -> dict:
        """每轮追加 perf_metrics.jsonl，返回当前指标。"""
    def check(self) -> Optional[dict]:
        """返回告警 dict 或 None。"""
```

**数据文件**：`data/bots/<id>/state/perf_metrics.jsonl`（append-only）
```json
{"ts": 1790618445, "cycle_id": "c-1", "decision": "hold", "executed": false,
 "pnl_usd": 0, "rolling_sharpe_20": 0.85, "win_rate_20": 0.55, "n_trades_20": 12}
```

**告警规则**（`check()`）：
- `rolling_sharpe < 0.5 × historical_sharpe`（historical 从 journal 全量算）
- `win_rate < 0.3` 且 `n_trades >= 10`
- `max_dd_30d < -15%`

**挂钩**：`PlanRunner.analyze_once` 成功后 → `detector.record_cycle(...)`；`check()` 非 None → `log.warning`。

### [S2.2] 订单状态机补全（order-state）

**修改文件**：`omnialpha/schema.py` + `omnialpha/executor.py`

```python
# schema.py 新增
ORDER_FINAL_STATES = {"filled", "canceled", "mmp_canceled"}
PARTIAL_FILL_STATUSES = {"partially_filled"}

# executor.py _open() 内，confirm 阶段
if order_state == "mmp_canceled":
    log.warning("mmp_canceled: %s", order_id)
    return StepResult(action, symbol, False, error="mmp_canceled")
if order_state == "partially_filled" and order_type == "ioc":
    filled = float(raw.get("accFillSz") or 0)
    log.info("IOC partial fill: %s/%s", filled, requested)
    # 记录实际成交量到 exec_result
```

**改单逻辑**：`modify_tp_sl` 分支（已有）→ 确认走「撤旧挂新」而非原地改（OKX 不支持原地改）。

### [S2.3] 监控告警（monitor）

**新增文件**：`omnialpha/monitoring/health.py`

```python
class HealthMonitor:
    def __init__(self, root: Path, bot_id: str):
        self.path = root / "data/bots" / bot_id / "state/health.json"
    def heartbeat(self, **metrics) -> None:
        """每轮更新 {ts, llm_latency, exec_latency, error_count, last_heartbeat}"""
    def check(self) -> list[str]:
        """返回告警消息列表。"""
```

**告警规则**：
- `llm_latency > 120s` → WARNING
- `error_count > 5`（连续）→ ERROR
- `last_heartbeat > 600s` → CRITICAL（进程可能挂了）
- `exec_latency > 30s` → WARNING

**挂钩**：`PlanRunner` LLM 调用前后计时 → `health.heartbeat(...)`。

### [S2.4] 波动率仓位管理（vol-sizing）

**修改文件**：`omnialpha/sizing.py` + `omnialpha/strategist/risk.py`

```python
# sizing.py 新增
def vol_adjust_size(base_size_usd: float, atr_pct: float,
                    target_pct: float = 2.0) -> float:
    """ATR 目标波动缩放。clamp 0.5x–2.0x。"""
    if atr_pct <= 0 or target_pct <= 0:
        return base_size_usd
    ratio = target_pct / atr_pct
    return base_size_usd * max(0.5, min(2.0, ratio))
```

**配置**：`strategist.risk.target_volatility_pct: 2.0`（默认 2%，0=禁用）

**挂钩**：`PlanRunner` 写 inbox 前 → `vol_adjust_size(size_usd, atr_pct, target_vol)`。

### [S2.5] 回测验证体系（backtest）

**新增文件**：`omnialpha/backtest/`（3 个模块）

```python
# replay.py
class BacktestReplayer:
    def __init__(self, root: Path, bot_id: str):
        ...
    def replay(self, days: int = 30) -> dict:
        """读 journal 逐 bar 模拟 → {trades, pnl_curve, metrics}"""
```

**数据源**：`memory_journal.jsonl`（`decision` + `exec_result`）+ 历史 K 线（Gate API）

**输出**：
```json
{
  "period": "30d", "n_cycles": 288, "n_trades": 12,
  "total_pnl_usd": 156.5, "win_rate": 0.58,
  "sharpe": 1.25, "max_dd_pct": -8.3,
  "benchmark_bh_pnl": 89.2, "benchmark_sma_pnl": 45.1,
  "dsr": 0.72, "p_value": 0.03
}
```

```python
# stats.py
def deflated_sharpe(returns: list[float], n_trials: int) -> float
def t_test_pvalue(returns: list[float]) -> float
def buy_and_hold_pnl(closes: list[float]) -> float
def sma_crossover_pnl(closes: list[float], fast: int = 50, slow: int = 200) -> float
```

**CLI**：`python -m omnialpha backtest --bot brooks-btc --days 30`

## [S3] Out of Scope

- 组合级风控（多 bot = 一仓位，已确认正确设计）
- TWAP/滑点保护 / self-critique / 多模态 / 防 MEV / DCA
- 回测 UI（CLI 输出即可）

## Tasks

- [x] T1: decay 检测器 — `monitoring/decay.py`：rolling_sharpe/win_rate/告警规则 (covers: S2.1)
- [x] T2: decay 挂钩 — `PlanRunner` 每轮 record_cycle + check (covers: S2.1; depends: T1)
- [x] T3: order-state 终态 — `schema.py` ORDER_FINAL_STATES + `executor.py` mmp_canceled/IOC (covers: S2.2)
- [x] T4: monitor 健康 — `monitoring/health.py` + heartbeat 挂钩 (covers: S2.3)
- [x] T5: vol-sizing — `sizing.py` vol_adjust_size + risk 配置 + 挂钩 (covers: S2.4)
- [x] T6: backtest replay — journal 回放 → trades/pnl_curve (covers: S2.5)
- [x] T7: backtest stats — DSR / p-value / B&H / sma 基准 (covers: S2.5; depends: T6)
- [x] T8: backtest CLI — `omnialpha backtest` 子命令 (covers: S2.5; depends: T6,T7)
- [x] T9: 测试 — 各模块单元 + 集成 (covers: S2.1–S2.5)
- [x] T10: 文档 — README 使用指南 (covers: S2.1–S2.5)
