# 自动化风控（Automated Risk Management）

## 风控状态机

```
状态机：
NORMAL → CAUTION → STOP_TRADING

状态转换条件：
NORMAL → CAUTION：
  - 连续亏损2笔
  - 日内亏损达到账户的2%
  - 市场进入窄区间（铁丝网）

CAUTION → NORMAL：
  - 盈利1笔后
  - 冷静15分钟后
  - 市场回到可交易状态

CAUTION → STOP_TRADING：
  - 连续亏损3+
  - 日内亏损达到账户的5%
  - 出现重大执行错误（不止损、报复性交易）

STOP_TRADING → NORMAL：
  - 次日重新开始
```

---

## 自动止损规则

### 止损拒绝条件

```
止损单被拒绝的条件（自动取消）：
1. 止损距离 > 账户的2% → 拒绝入场
2. 止损距离 < 最小波动 × 3 → 拒绝入场（止损太近）
3. 止损在整数关口/磁铁的正上方/下方 → 调整到磁铁外1-2tick
```

### 止损入场单10条规则

```
1. 止损单只用于突破和强趋势
2. 限价单只用于回调和区间
3. 止损单位置必须在信号K线极值外
4. 止损单不得在窄区间中使用
5. 止损单入场后立即设置OCO
6. 止损单触发后不追单（除非有BOP信号）
7. 止损单在重大新闻前15分钟取消
8. 止损单在流动性差的时间段（如开盘前5分钟）不使用
9. 止损单距离必须合理（1-2倍ATR内）
10. 止损单不得在已亏损的情况下加仓
```

---

## 自动仓位调整

### 动态仓位调整

```
当日首笔交易：标准仓位 × 0.75（保守开局）
当日盈利 > 2%：标准仓位 × 1.0（正常）
当日亏损 > 2%：标准仓位 × 0.5（减仓保护）
连续盈利3笔：标准仓位 × 1.0（不增加，防止过度自信）
连续亏损2笔：冷静15分钟 + 仓位 × 0.5
```

### 最大回撤保护

```
日内最大回撤 = 账户的5%
如果日内回撤达到5%：
  1. 立即平掉所有仓位
  2. 当日不再交易
  3. 记录到错误日志
  4. 次日复盘后再决定是否恢复交易

周最大回撤 = 账户的10%
如果周回撤达到10%：
  1. 暂停交易1-2天
  2. 深度复盘本周所有交易
  3. 审查系统是否有效
  4. 减仓至50%恢复交易
```

---

## 紧急风控触发

### 触发条件

```
1. 价格剧烈波动（单根K线振幅 > 5×ATR）
2. 出现极端缺口（缺口 > 3×ATR）
3. 交易所异常（断线、暂停交易）
4. 重大新闻事件（如利率决议、非农数据）
5. 清算级联（加密货币特有的连续清算）
```

### 紧急处理

```
1. 立即平掉所有持仓
2. 取消所有挂单
3. 停止交易30分钟
4. 重新评估市场状态
5. 确认安全后恢复交易（仓位减半）
```

---

## 自动化风控规则汇总

```
伪代码：
function risk_check(account_state, market_state, trade_signal):
    // 1. 风控状态检查
    if account_state.risk_state == STOP_TRADING:
        return REJECT("当日已停止交易")
    
    if account_state.risk_state == CAUTION:
        if trade_signal.priority < 3:
            return REJECT("谨慎状态下仅接受优先级3+的信号")
    
    // 2. 连续亏损检查
    if account_state.consecutive_losses >= 2:
        return REJECT("连续亏损，需冷静15分钟")
    
    // 3. 日内亏损检查
    if account_state.daily_loss >= account_state.max_daily_loss:
        return REJECT("日内亏损已达上限")
    
    // 4. 止损距离检查
    if trade_signal.stop_loss_distance > account_state.balance * 0.02:
        return REJECT("止损距离超过账户2%")
    
    // 5. 市场状态检查
    if market_state.state == TIGHT_RANGE:
        return REJECT("窄区间不交易")
    
    // 6. 仓位计算
    position = calculate_position(account_state, market_state, trade_signal)
    
    // 7. 总风险检查
    if position.risk > account_state.balance * 0.02:
        position = adjust_position(position, max_risk = account_state.balance * 0.02)
    
    return APPROVE(position)
```
