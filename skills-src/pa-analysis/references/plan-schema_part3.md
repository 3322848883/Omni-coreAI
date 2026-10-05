> 本文件是 `plan-schema.md` 的第 3/6 片（按 `##` 小节切分，内容未改动）。

## 信号K params 词表规范（signal_bar / sequence，与节点3 signal_eval.py 逐字一致）

> 来源：align-signal-bar-coverage spec（信号K覆盖对齐 Task 4 契约硬化）。本节词表与节点3 `pa-executor/core/signal_eval.py` 实际枚举**逐字一致**——写计划时 pattern/sequence 名必须取自下表，**未登记名节点3 一律 manual（转监控员 AI），无 AI 时计划过期**。分工定纲：**程序能识别的 → 参数化全自动；程序不能判定的 → 明确标注 `ai_judged: true` 归口监控员 AI，不留灰色地带**。45 项知识库形态↔程序对照矩阵见 `references/pattern-catalog.md` 第五节。

### signal_bar pattern 枚举（11 种）

| pattern | 判定语义 | 专属 params |
|---|---|---|
| `shooting_star` | 射击之星：实体≤50% + 上影≥33% + 收在下端（theme15 15.5 长上影变体，对齐「上尾线约1/3~1/2」下界） | — |
| `hammer` | 锤子：实体≤50% + 下影≥33% + 收在上端（theme15 15.4 长下影变体，对齐「下尾线约1/3~1/2」下界） | — |
| `doji` | 十字星：实体≤25%（多空均衡） | — |
| `inside` | 内包：高点≤前高 且 低点≥前低（力量收缩） | — |
| `outside` | 外包：高点≥前高 且 低点≤前低（波动扩大，收盘定方向） | `direction`（bull→OU 收阳外包 / bear→OD 收阴外包；**不填保持纯几何行为**） |
| `engulfing` | 吞没：本棒整根高低点包络前棒（文件16 四.2b「完全包含，类似外包」） | — |
| `strong_wide_range` | 强宽幅棒：range≥ATR14 且 实体≥70%（统一大实体口径） | — |
| `bull_bar` | 多头K线最小标准：收盘>开盘 或 收盘>中点 | — |
| `bear_bar` | 空头K线最小标准：收盘<开盘 或 收盘<中点 | — |
| `trend_bar` | 趋势棒（文件16 三.1 相对均值口径）：实体>近 `lookback` 根均实体×1.5 + 收盘距极点<20%实体 + 方向收盘；**direction 必填** | `lookback`(默认 20，前K窗口) |
| `reversal_bar` | 最佳反转棒：**最小特征强制**（bull 收阳或收中点上 / bear 收阴或收中点下，theme15 15.4/15.5）+ 拒绝侧影线 ∈ [`tail_ratio_min`,`tail_ratio_max`]（可叠收取回前收/低重叠） | `tail_ratio_min`(默认 0.33)、`tail_ratio_max`(默认 0.60，对齐「约1/3~1/2」+0.1 容差)、`reversal_prior_close`(bool)、`overlap_ratio_max` |

**signal_bar 通用 params（可与任一 pattern 叠加，met=全部 check 通过）**：`body_ratio_min`、`close_position_min`(bull 收上端下限)、`close_position_max`(bear 收下端下限)、`max_range_atr`、`within_entry_zone`(bool)、`or_long_wick`(bull 长上影豁免)、`or_lower_wick`(bear 长下影豁免)、`direction`(bull/bear)、`timeframe`(可选，选K线周期)。`bull_bar`/`bear_bar` 叠加 `body_ratio_min` 即升格趋势棒判定；**不带 `pattern` 时为通用几何信号K**（阈值组合判定，#0d）。

