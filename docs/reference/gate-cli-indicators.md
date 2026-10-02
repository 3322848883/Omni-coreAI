# Gate CLI / Gate Skills 指标能力核实（2026-09-24）

来源：本机 `gate-cli.exe info mcp-spec`（offline）+ `gate/gate-skills` 本地克隆  
本机可参考 gate-skills 仓库（路径随克隆位置而定）与 https://github.com/gate/gate-skills

## 1. gate-cli 指标序列（`info_markettrend_get_indiator_history`）

命令：

```bash
gate-cli info markettrend get-indicator-history \
  --symbol BTC_USDT --timeframe 1d --limit 100 \
  --indicators rsi --indicators macd --indicators close_price
```

| 项 | 官方支持 |
|----|----------|
| **指标名（17）** | `rsi`, `macd`, `macd_dea`, `macd_difference`, `ma7`, `ma30`, `ma120`, `ma200`, `ema7`, `ema30`, `ema120`, `ema200`, `boll_middle_band`, `boll_upper_band`, `boll_lower_band`, `close_price`, `volume` |
| **timeframe** | 仅 `15m` / `1h` / `4h` / `1d` |
| **limit** | 默认 100，最大 500 |
| **周期自定义** | **否** — MA/EMA 固定 7/30/120/200；RSI/MACD/BOLL 固定默认参 |
| **ATR** | **无** |
| **Stochastic / KDJ / CCI / VWAP…** | **无** |

说明（help 原文）：indicators 为 ES `_source` 名，**非封闭枚举**，以上为 `mcp-spec` 列出的 `common_values`。

### 其它「类指标」能力（不是指标序列）

| 来源 | 内容 |
|------|------|
| `get-technical-analysis` | 15m/1h/4h/1d 多周期综合信号（服务端聚合） |
| `gate-exchange-marketanalysis` | **13 场景**：流动性/动量买卖比/爆仓/资金费率套利/基差/操纵风险/盘口解释/滑点模拟/突破与支撑阻力/周末量能/技术建议/多资产配置/组合再平衡 |
| `gate-info-trendanalysis` | RSI、MACD、BOLL、MA 报告模板 + 支撑阻力 |
| cex REST | `funding-rate` / `liquidations` / `premium` / orderbook / trades（微观结构，非技术指标） |

gate-cli Info 域共 **35 个 MCP 工具**；gate-skills 技能 **50+**（含交易/理财/链上/新闻等，指标相关主要是 trendanalysis / coinanalysis / marketanalysis）。

## 2. 本项目（OmniAlpha）已支持

| 指标 | 快照 `market.indicators` | 触发 `conditions` | 周期 |
|------|--------------------------|-------------------|------|
| **EMA** | `emaN` | `price_vs_ema`, `ema_cross` | **任意 N** |
| **RSI** | `rsiN` | `rsi` (gt/lt) | **任意 N** |
| **ATR** | `atrN` | `atr_spike` | **任意 N** |
| 价格突破 | — | `price_break` | `lookback` 任意 |

另有：funding/mark/index、OI、多空比、盘口 5 档（`market.refresh`）。

## 3. 对比结论

| 能力 | Gate CLI / Skills | 本项目 |
|------|-------------------|--------|
| 指标个数 | **17 个序列名**（4 组族） | **3 族任意周期**（ema/rsi/atr 组合可到任意多个） |
| EMA 周期 | 固定 7/30/120/200 | **任意**（9/20/21/50…） |
| RSI 周期 | 固定 | **任意** |
| ATR | ❌ | ✅ 任意周期 + spike 条件 |
| MACD / BOLL | ✅ | ❌（未做） |
| MA 简单均线 | ✅ 7/30/120/200 | ❌（可用 EMA 近似） |
| 多周期合成信号 | ✅ technical_analysis | ❌（本地多 timeframe 配置） |
| 微观结构场景 13 类 | ✅ marketanalysis | 部分（盘口/资金费/OI 进快照） |
| 条件触发引擎 | ❌（技能只出报告） | ✅ 定时/收盘/指标条件 |

**互补**：CLI 强在 MACD/BOLL/固定均线与报告合成；本项目强在 **任意周期 EMA/RSI/ATR + 可编程事件触发 + 直接下单**。

## 4. 若要对齐 Gate 缺口（可选 P1）

1. 快照/触发增加 `macd`（需 3 输出）、`bollN,k`、`maN`
2. 触发增加 `macd_cross`、`boll_break`
3. 旁路调用 `get-indicator-history` 填充快照（受 Intel 通道稳定性限制）
