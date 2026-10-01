# 实战行情分析完整流程

> 本文件记录了使用 price-action-trading 技能分析实时行情的完整步骤，基于 ETH/USDT 实战验证。

## 数据加载

行情 K 线数据目录由 `PRICE_ACTION_DATA_DIR` 指定（默认 `<skill>/data`），JSON 格式：
- 字段：`t`(unix秒), `o`, `h`, `l`, `c`, `ema20`, `atr14`, `v`(成交量), `sum`(成交额)
- 时间框架：`{instrument}_{timeframe}.json`，如 `eth_usdt_5m.json`
- 使用 `execute_code` + Python 读取（Windows terminal 有 WSL bash 问题）

## 分析流程（10 步）

### 第 1 步：加载数据
```python
# 加载 3 个时间框架
eth_4h = load_json("eth_usdt_4h.json")  # HTF 背景
eth_1h = load_json("eth_usdt_1h.json")  # 中间框架
eth_5m = load_json("eth_usdt_5m.json")  # 主交易框架
```

### 第 2 步：记忆检索（Step 0）
读取 `memory/` 下 5 个文件：
- trader_profile.md → 个人画像
- pattern_effectiveness.md → 设置有效性
- error_patterns.md → 错误模式
- market_wisdom.md → 市场智慧
- strategy_hypotheses.md → 策略假设

### 第 3 步：4H HTF 分析
- 打印最近 20 根 K 线，逐根看收盘 vs EMA20
- 统计：收盘 > EMA 的比例 → 判定 AIS/AIL
- EMA 方向：5 根前 vs 当前的变化量
- 最新 K 线类型（多/空/十字星）

### 第 4 步：1H 分析
- 打印最近 20-30 根
- 统计 HH/LL 比例（LL > HH = 空头）
- 收盘 vs EMA 比例
- EMA 方向

### 第 5 步：5M 主图分析（3 轮）
**轮 1：统计** — 最近 40-60 根的多空比、收盘>EMA、ATR
**轮 2：逐 K 线** — 打印最近 40 根，看实体/影线/收盘 vs EMA
**轮 3：波段结构** — 用 3-bar pivot 算法找 SH/SL：
```python
for i in range(1, len(bars)-1):
    if h[i] > h[i-1] and h[i] > h[i+1]:  # Swing High
    if l[i] < l[i-1] and l[i] < l[i+1]:  # Swing Low
```
分析 LH/LL/HH/HL 序列判定趋势方向

### 第 6 步：决策树走查（strategy_workflow 第 2 章）
```
Q1: 有趋势？→ 4H/1H/5M 分别判定
Q2: 趋势强度？→ 强/弱/通道
Q5: Always In？→ AIS/AIL/不确定
Q7: 当前位置？→ 区间极端/中间/EMA 附近
```
→ 进入对应规则引擎（第 3/4/5 章）

### 第 7 步：建仓形态扫描
在 5M 图上扫描：H2/L2、均线回撤、失败 Low2、双底/双顶等
参考 strategy_workflow 第 6 章优先级表

### 第 8 步：交易者方程（strategy_workflow 第 11 章）
对每个方案计算 `P(win)×R > P(loss)×Risk`

### 第 9 步：禁止清单检查（strategy_workflow 第 12 章）
BAN-001（区间中间不交易）、BAN-002（铁丝网）等

### 第 10 步：输出交易计划
- 判定：交易/等待
- 等待的触发条件（精确价位）
- 入场/止损/目标/仓位

## 关键分析模式

### EMA20 磁铁位置
当价格恰好在 5M EMA20 上时，这是**决策等待区**：
- 等待空头信号棒确认 EMA 压制 → 做空
- 或等待价格突破 EMA + 回踩确认 → 做多
- **不在 EMA 上直接入场**

### ATR 压缩
ATR < 1.5（5M 图）→ 波动率压缩，预示即将突破
→ 等待突破方向确认再入场

### 弱空头趋势中的 HL 序列
当 SH 为 LH（空头）但 SL 为 HL（多头尝试）时：
→ 弱空头趋势，可能正在构筑底部
→ 优先做空但减少仓位，或等待底部确认

## 输出格式

分析报告按 SKILL.md 的阶段结构组织：
- 【阶段零】盘前准备
- 【阶段一】市场分析（含决策树走查结果）
- 【阶段二】开盘专项
- 【阶段三】信号识别（含形态扫描 + 交易者方程）
- 【阶段四】交易计划（含精确价位）
- 【阶段五】管理规则
- 核心结论 + 知识来源引用表
