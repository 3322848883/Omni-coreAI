# 场景 A：自然触发（不强制点名 skill，看 catalog 能否驱动）
你是量化策略引擎。用户请求：**分析 ETH_USDT 当前 1h 行情，给出交易倾向**。

按系统里提供的技能目录决定是否加载相应技能；若加载了技能，用其方法论分析。
最后输出 Plan JSON（hold/open_long/open_short），reasoning 用中文写价格行为结论。
允许动作：hold / open_long / open_short。名义极小，安全。
