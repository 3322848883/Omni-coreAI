# TV 三张截图 ↔ 移植实现 核对清单

数据基准：`verify_data/tv_inputs.json`（BTC_USDT 1h，60 根，Gate kline.db）
参数对齐截图标题栏：RSI Yata `14 close 3 21 SMA 2`，MACD `12 26 close 9 EMA EMA`，通道 3 层。

---

## 图 1 — RSI Yata

| 截图元素 | 实现 | 末根数值（bar 59, 2026-09-25 22:00） | 状态 |
|----------|------|--------------------------------------|------|
| 紫线 RSI(14) | `rsi_base(c, 14)` | **44.5645** | ✅ |
| 中线 MA(21 SMA) | `rsi_ma(rsi, 21, "SMA")` | **45.8926** | ✅ |
| 布林带 ±2σ | `rsi_bollinger(rsi, 21, 2.0)` | upper **55.4773** / lower **36.3079** | ✅ |
| HH / HL / LH / LL 标签 | `swing_structure(rsi, 3, 3)` | 见下方结构序列 | ✅ 补齐 |
| RSI-MACD(12,26,9) | `rsi_macd(rsi, 12, 26, 9)` | hist **-1.0954** | ✅ 补齐 |
| OB/OS 点 | `ob_os_signals(rsi, 70, 30)` | — | ✅ |
| 底部 RSI 蜡烛 | `rsi_candles(rsi)` | — | ✅ |
| RSI 直方图 | `rsi_histogram(rsi, ma)` | **-1.3281** | ✅ |

**swing_structure 标签序列（对照截图 LH/HH/HL/LL 气泡）：**

| bar | time UTC | RSI | 标签 |
|----:|----------|----:|------|
| 20 | 09-24 07:00 | 41.7734 | H |
| 22 | 09-24 09:00 | 30.2477 | L |
| 31 | 09-24 18:00 | 50.1823 | **HH** |
| 35 | 09-24 22:00 | 47.7230 | **HL** |
| 38 | 09-25 01:00 | 52.6265 | **HH** |
| 44 | 09-25 07:00 | 43.3631 | **LL** |
| 47 | 09-25 10:00 | 56.6854 | **HH** |
| 53 | 09-25 16:00 | 39.2767 | **LL** |
| 56 | 09-25 19:00 | 45.6194 | **LH** |

---

## 图 2 — LR HA Candles（B3AR_Trades）

| 截图元素 | 实现 | 末根数值 | 状态 |
|----------|------|----------|------|
| 海蜡烛 HA close/open | `heikin_ashi(o,h,l,c)` | close **83821.5000** / open **83840.4274** | ✅ |
| 线性回归平滑 HA | `lr_ha_candles(o,h,l,c, 9)` | close **83874.1978** / open **83784.8625** | ✅ |
| T3 均线 (5, 0.7) | `t3_moving_average(c, 5, 0.7)` | **83796.8035** | ✅ |
| 波动带 basis/±2ATR | `volatility_bands(h,l,c, 20)` | basis **84017.7290** / upper **84901.4586** | ✅ |
| LR 蜡烛 (非 HA) | `lr_candles(o,h,l,c, 9)` | — | ✅ |

---

## 图 3 — Linreg & Trendlines（ParkF）三层通道

截图：粗中轴 + 实线/虚线/点线三层偏差带 + 枢轴趋势线。

| 截图元素 | 实现 | 末根数值（窗口=最后20根） | 状态 |
|----------|------|---------------------------|------|
| 中轴 base | `linreg_channel(...).base` | start **84342.8129** → end **83807.4171** | ✅ |
| 内层 1σ（实线） | `layers[0]` | upper **84051.4455** / lower **83563.3888** | ✅ 补齐 |
| 中层 2σ（虚线） | `layers[1]` | upper **84295.4738** / lower **83319.3605** | ✅ 补齐 |
| 外层 3σ（点线） | `layers[2]` | upper **84539.5022** / lower **83075.3321** | ✅ 补齐 |
| slope | `calc_slope` | **-28.178722** | ✅ |
| std_dev | `calc_dev` | **244.0283** | ✅ |
| pearson_r | `calc_dev` | **0.564086** | ✅ |
| 枢轴趋势线 | `trendlines(h,l,c,o, 10)` | primary/secondary upper+lower | ✅ |

**三层关系自检：** upper_k = base_end + k × std_dev
- 1σ: 83807.4171 + 244.0283 = 84051.4455 ✅
- 2σ: 83807.4171 + 488.0566 = 84295.4738 ✅
- 3σ: 83807.4171 + 732.0850 = 84539.5022 ✅

---

## 上 TV 怎么对

1. **Pine 脚本法（精确）**：`verify_data/tv_pine_check.pine` 粘进 Pine Editor，表格数值应与上表逐位一致。
2. **图表目测法（形态）**：
   - 图1：RSI 面板上 HH/HL/LH/LL 气泡位置 ↔ 上表标签序列
   - 图2：青色 LRHA 与原蜡烛的偏离幅度
   - 图3：三层通道宽度比应为 **1 : 2 : 3**（内:中:外）
3. **交易所差异**：Gate vs TV 数据源个别根可差 0.0x，算法一致性以固定数据为准。

---

## 本轮补齐项（对照截图发现的缺口）

| 缺口 | 补丁 | 测试 |
|------|------|------|
| 通道只有单层，截图是三层 | `linreg_channel(..., layers=(1,2,3))` | `test_linreg_channel_three_layers` |
| 无 HH/HL/LH/LL 结构标签 | `swing_structure()` | `TestSwingStructure` 3 项 |
| 无 RSI-MACD | `rsi_macd()` | `TestRsiMacd` 2 项 |

一致性测试 **31 OK**；全量 **701 OK**。
