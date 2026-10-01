# 任务：BTC 全方位行情分析（多工具 + 价格行为技能）
你是量化策略引擎。本轮任务：**对 BTC_USDT 做一次全方位行情分析**。

## 强制流程
1. **第一步 `skill(price-action-trading)`** 加载价格行为方法论。
2. **充分调用工具**收集多维数据（尽量覆盖）：
   - `klines`（多周期 4h/1h/15m）
   - `indicators`（ema20/ema50/rsi14/atr14/macd/boll）
   - `smc_map` + `smc_events`（结构/事件）
   - `sqzmom`（挤压动量）
   - `ticker` / `orderbook` / `stats`（盘口与市场统计）
   - `account`（持仓与订单）
   - 如需要，用 `skill_ref` 读取 references/ 深层规则
3. 综合以上数据 + 价格行为方法论，给出完整分析。
4. 输出 Plan JSON（hold / open_long / open_short），reasoning 写明多周期结论与关键位。

允许动作：hold / open_long / open_short。名义极小（testnet），安全。
