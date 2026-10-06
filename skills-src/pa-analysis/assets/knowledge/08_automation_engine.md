# 自动化决策引擎（Automation Engine）

## 概述

本模块将概念性知识转化为可编程的量化规则，使AI能够自动判断市场状态、评估信号、做出决策，无需人工干预。

---

## 一、市场状态自动检测

### 1.1 趋势 vs 区间 检测算法

```
输入：最近N根K线的OHLC数据（默认N=20）
输出：TREND_UP / TREND_DOWN / RANGE

步骤：
1. 计算摆动高点和摆动低点（5根K线窗口）
2. 检查HH+HL序列（多头趋势）或 LH+LL序列（空头趋势）
3. 计算K线重叠率 = 重叠K线数 / 总K线数
4. 计算EMA位置

判定规则：
- 重叠率 < 30% 且 HH+HL ≥ 3次 → TREND_UP
- 重叠率 < 30% 且 LH+LL ≥ 3次 → TREND_DOWN
- 重叠率 ≥ 60% → RANGE
- 30% ≤ 重叠率 < 60% → 边界情况，结合EMA判断
```

### 1.2 突破 vs 通道 检测算法

```
输入：已判定为趋势的K线序列
输出：BREAKOUT / TIGHT_CHANNEL / BROAD_CHANNEL

判定规则：
- 连续趋势K线数量 ≥ 5 且 平均重叠率 < 10% → BREAKOUT
- 平均回撤K线数 ≤ 3 且 最大回撤距离 < 通道宽度的40% → TIGHT_CHANNEL
- 平均回撤K线数 > 3 或 最大回撤距离 ≥ 通道宽度的40% → BROAD_CHANNEL
```

### 1.3 宽区间 vs 窄区间 检测算法

```
输入：已判定为区间的K线序列
输出：BROAD_RANGE / TIGHT_RANGE

判定规则：
- 区间高度 > ATR(14) × 2 → BROAD_RANGE
- 区间高度 ≤ ATR(14) × 2 且 K线重叠率 > 80% → TIGHT_RANGE
- 区间高度 ≤ ATR(14) × 2 且 K线重叠率 ≤ 80% → BROAD_RANGE
```

### 1.4 小反转 vs 大反转 检测算法

```
输入：趋势中出现的反向走势
输出：MINOR_REVERSAL / MAJOR_REVERSAL

判定规则：
- 反向走势触及主要趋势线 且 未突破 → MINOR_REVERSAL
- 反向走势突破主要趋势线 → 检查是否测试旧极端
  - 是 → MAJOR_REVERSAL
  - 否 → 继续观察，暂定为MINOR_REVERSAL
```

### 1.5 量化阈值汇总

| 检测项 | 阈值 | 条件 |
|--------|------|------|
| 趋势重叠率 | < 30% | 趋势成立 |
| 区间重叠率 | ≥ 60% | 区间成立 |
| 突破连阳数 | ≥ 5根 | 突破成立 |
| 突破重叠率 | < 10% | 突破持续 |
| 窄通道回撤 | ≤ 3根 | 窄通道成立 |
| 宽通道回撤 | > 3根 | 宽通道成立 |
| 宽区间高度 | > 2×ATR | 宽区间成立 |
| 窄区间重叠率 | > 80% | 窄区间成立 |
| 反转破趋势线 | 突破 | 大反转 |
| 反转未破趋势线 | 未突破 | 小反转 |

---

## 二、信号棒自动判断（Brooks 原文定性）

> Brooks 不使用打分系统，自动化判断需基于原文定性规则，而非数值评分。

### 2.1 可编程判断规则

```
输入：信号棒的OHLC + 前一根棒的OHLC + 市场背景
输出：强 / 中 / 弱

判断逻辑（基于Brooks原文V1 P117-P118）：

1. 收盘位置：
   - 多头：收盘高于前一根收盘 且 收盘高于多根棒线最高价 → 强
   - 空头：收盘低于前一根收盘 且 收盘低于多根棒线最低价 → 强
   - 收盘在棒线中间 → 弱

2. 影线质量：
   - 多头：上影线短或不存在，下影线约1/3到1/2高度 → 强
   - 空头：下影线短或不存在，上影线约1/3到1/2高度 → 强
   - 入场方向影线过长 → 弱

3. 实体大小：
   - 中等实体 → 强
   - 十字星 → 弱（单棒交易区间）
   - 极大实体 → 弱（可能是耗尽）

4. 与前棒重叠：
   - 与前棒无重叠 → 强
   - 与前两棒有较大重叠 → 交易区间的一部分，弱
   - 多头反转棒中点高于前一棒低点 → 重叠过多，弱

5. 位置优势：
   - 在磁铁/支撑/阻力/EMA关键位 → 强
   - 在无意义区域 → 弱

6. 趋势位置：
   - 趋势早期 + 顺势 → 强
   - 趋势过度延伸 + 逆势 → 弱（大多数反转失败）
```

### 2.2 信号组合自动判定

```
输入：所有检测到的信号 + 市场状态
输出：信号优先级（1-7）

算法：
1. 基础判断 = 信号棒强度（强/中/弱）
2. 如果信号方向 = Always In方向 → 优先级提升
3. 如果有第二个信号确认 → 优先级提升
4. 如果在磁铁/支撑/阻力附近 → 优先级提升
5. 如果信号组合 = H2+趋势方向+磁铁 → 优先级1（最高）
6. 如果信号组合 = H1+趋势方向 → 优先级2
7. 如果信号组合 = 双顶/底+区间边界 → 优先级3
8. 如果信号组合 = 楔形+第二个信号 → 优先级4
9. 如果仅单一信号 → 优先级5-6
10. 如果逆趋势 → 优先级7（最低）
```
## 三、仓位自动计算

```
输入：账户余额、状态、信号级别、止损距离
输出：仓位大小

公式：
标准仓位 = 账户余额 × 0.02 / 止损距离
实际仓位 = 标准仓位 × 状态系数 × 级别系数

状态系数：
- 突破：0.5-0.75
- 窄通道：1.0
- 宽通道(H2)：1.0-1.25
- 小反转：0.75
- 大反转：0.5
- 宽区间：1.0
- 窄区间：0

级别系数：
- 5级：1.0
- 4级：1.0
- 3级：0.75-1.0
- 2级：0.5
- 1级：0.25-0.5
- 0级：0

最大仓位限制：
- 单笔最大风险：账户的2%
- 单日最大亏损：账户的3-5%
- 如果日内已亏损 ≥ 3% → 停止交易
```

---

## 四、自动化决策流程

```
伪代码：
function auto_trading_decision(klines_data, account_data):
    // Step 1: 检测市场状态
    state = detect_market_state(klines_data)
    
    // Step 2: 检测信号
    signals = detect_signals(klines_data, state)
    
    // Step 3: 判断信号棒强度（强/中/弱）
    qualified_signals = qualify_signals(signals, state)
    
    // Step 4: 过滤信号（仅保留强/中等信号）
    valid_signals = filter(qualified_signals, quality >= moderate)
    
    // Step 5: 排序信号（按优先级）
    sorted_signals = sort_by_priority(valid_signals)
    
    // Step 6: 检查交易前10项清单
    if not check_10_point_checklist(state, sorted_signals[0], account_data):
        return NO_TRADE
    
    // Step 7: 计算仓位
    position = calculate_position(account_data, state, sorted_signals[0])
    
    // Step 8: 设置止损止盈
    stop_loss = calculate_stop_loss(state, sorted_signals[0])
    take_profit = calculate_take_profit(state, sorted_signals[0], position)
    
    // Step 9: 生成OCO订单
    order = generate_oco_order(position, stop_loss, take_profit)
    
    return order
```
