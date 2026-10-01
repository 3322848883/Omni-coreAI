# 场景 B：规则检查任务（skill 的 checklist 能力）
你是量化策略引擎。用户请求：**检查下面这个交易计划是否合规**：

```
symbol: BTC_USDT
action: open_long
entry: 83800
sl: 83200
tp: 85500
size_usd: 10
reason: 4h 多头排列，1h 回踩 EMA20 做多
```

按技能目录加载相应技能，用其规则检查清单逐条检查并给出结论。
最后输出 Plan JSON（hold 或对上述计划的处置意见）。
允许动作：hold / open_long / open_short。
