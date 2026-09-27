# 指标 / 触发 / 快照 — 深度支持矩阵与缺口（2026-09-27）

实测代码行为（`parse_indicator_name` / `attach_indicators` / `evaluate_condition`）。

## 1. 总览

| 层 | 已实现 | 静默忽略/缺失 | 与 Gate CLI 对齐 |
|----|--------|---------------|------------------|
| **指标计算** | EMA/SMA/RMA/WMA/VWMA/RSI/ATR（**任意周期**）+ MACD/BOLL + **Stoch/CCI/WR/MFI/ADX/VWAP/OBV/SuperTrend** | — | 超集 |
| **ATR** | Pine `ta.tr(true)` + `rma\|sma\|ema\|wma` 平滑（`atr14`/`atr14_ema`…） | — | CLI 无 ATR |
| **快照** `market.indicators` | 全部上述名 | 未知名报错（不再静默 null） | — |
| **触发** `conditions` | 5 种（EMA 单边/交叉、RSI、ATR spike、价格突破） | 无 macd_cross / boll_break / ma_cross / 量能 | Skills 只出报告，无触发引擎 |
| **P1 行情** | funding/mark/index、OI、多空比、盘口5档 | premium、basis、买卖比成交流 | marketanalysis 13 场景更全 |
| **测试** | 107 单测（EMA/RSI/ATR/5条件） | MACD/BOLL/MA **0 测试** | — |

## 0. 真实数据验证记录（testnet BTC_USDT 15m，120 根）

| 项 | 实测结果 |
|----|----------|
| 自定义周期 | `ema9=83987.6` / `ema21=83868.2` / `rsi7=52.8` / `atr10=389.3` ✅ |
| 标准周期 | `ema20` / `ema50` / `rsi14` / `atr14` ✅ |
| 静默忽略 R1 | `macd/boll_upper_band/ma7/sma20/foo` → **全 null，无告警** ⚠️ |
| 条件真实触发 | `rsi`/`atr_spike` fired=True；`price_vs_ema`/`ema_cross`/`price_break` 按行情未触发但返回 reason ✅ |
| 缺失条件 | `volume_spike`/`macd_cross`/`boll_break`/`ma_cross` → `unknown condition` ⚠️ |
| Gate CLI 名称 | `rsi,macd,ema7,boll_upper_band,ma7,close_price` **均被接受**（返回 count=0，Intel 空数据） |
| 单测 | **107 OK** |
| 生产套件 testnet + 真实 AI | **24/24 PASS**（含 interval/kline_close 触发 + 全订单） |

## 2. 指标名解析矩阵（实测）

| 名称 | 解析结果 | 快照可算 | 备注 |
|------|----------|----------|------|
| `ema7/9/20/50/120`… | `('ema', N)` | ✅ | **任意 N**，优于 CLI 固定 7/30/120/200 |
| `rsi7/14`… | `('rsi', N)` | ✅ | 任意 N |
| `atr10/14`… | `('atr', N)` | ✅ | CLI **没有 ATR** |
| `ma7` / `sma20` | `('', 0)` | ❌ **静默 null** | CLI 有 ma7/30/120/200 |
| `macd` / `macd_dea` / `macd_difference` | `('', 0)` | ❌ **静默 null** | CLI 有 |
| `boll20` / `boll_upper`… | `('', 0)` | ❌ **静默 null** | CLI 有 boll_*_band |
| `stoch` / `cci` / `vwap` / `obv` / `wr` / `mfi` / `adx` | `('', 0)` | ❌ | CLI 也没有 |

**风险 R1（静默忽略）**：`MarketConfig(indicators=['macd','foo'])` 会接受配置，但 `latest_indicators` 填 `null`，**不警告**。策略以为有 MACD，实际没有。

## 3. 触发条件矩阵

| type | 参数 | 测试 | 状态 |
|------|------|------|------|
| `price_vs_ema` | period, side | ✅ | 完成 |
| `ema_cross` | fast, slow, dir | ✅ | 完成 |
| `atr_spike` | period, mult, lookback | ✅ | 完成 |
| `rsi` | period, op, level | ✅ | 完成 |
| `price_break` | lookback, side | ✅ | 完成 |
| `macd_cross` | fast, slow, signal, dir | ❌ | **缺** |
| `boll_break` | period, k, side | ❌ | **缺** |
| `ma_cross` | fast, slow, dir | ❌ | **缺**（可用 ema_cross 代） |
| `volume_spike` | mult, lookback | ❌ | **缺** |
| `all`/`any` 组合 | — | ❌ | **缺**（多条件目前是 OR） |

## 4. Gate CLI / Skills 侧（对照）

| | CLI 指标序列 (17) | marketanalysis 13 场景 | 本项目 |
|--|-------------------|------------------------|--------|
| RSI/MACD/BOLL/MA | ✅ 固定参 | 报告合成 | 部分（RSI 任意） |
| ATR | ❌ | ❌ | ✅ 任意 |
| 自定义周期 | ❌ | — | ✅ |
| 滑点/基差/操纵/资金费套利 | 场景分析 | ✅ | 仅 funding/OI/盘口进快照 |
| 条件→下单 | ❌ | ❌ | ✅ |

## 5. 缺口优先级（建议补齐顺序）

| 优先 | 项 | 收益 | 工作量 |
|------|----|------|--------|
| **P0** | 未知指标名 **报错/警告**（修 R1 静默 null） | 防策略误配 | 小 |
| **P0** | `maN`/`smaN`（简单均线） | 对齐 CLI ma7/30/120/200 | 小 |
| **P1** | `macd`（dif/dea/hist）+ `macd_cross` 触发 | 对齐 CLI/Skills 主指标 | 中 |
| **P1** | `bollN,k`（上中下轨）+ `boll_break` 触发 | 对齐 CLI | 中 |
| **P2** | `volume_spike`、`all`/`any` 组合条件 | 表达力 | 中 |
| **P2** | Stoch/CCI/VWAP | CLI 也没有 | 按需 |
| **P3** | 旁路拉 `get-indicator-history` | 复用 Gate 计算，省本地算 | 依赖 Intel 稳定性 |

## 6. 测试覆盖现状

| 模块 | 测试数 | 覆盖 |
|------|--------|------|
| indicators + market + snapshot | 27 | EMA/RSI/ATR/warm-start/自定义周期 |
| triggers | 8 | 5 种条件 + cooldown |
| strategist loop | 18 | 触发/cycle/abort |
| 契约 kline | 6 | schema v1 |
| 执行/schema | 55 | 全订单类型 |
| **macd/boll/ma** | **0** | 未实现故无测 |

单测合计 **107 OK**；生产 harness：`test_production.py` 24 项、`test_full_chain.py` 10 项。

## 7. 结论

- **已支持且优于 CLI**：任意周期 EMA/RSI、**ATR**、条件触发引擎  
- **明确缺口**：MACD、BOLL、MA/SMA、未知名静默 null、组合条件、量能触发  
- **建议先修 P0 静默忽略 + 补 MA**，再上 MACD/BOLL（P1）
