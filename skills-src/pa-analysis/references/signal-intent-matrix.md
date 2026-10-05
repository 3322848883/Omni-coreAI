# 信号 → 订单意图对照表（63 条）

> 执行侧交易意图表（与执行侧枚举逐字一致）。写证据链时用它确认「这个信号对应什么订单意图」，**但它不替代你的判断** —— 方案仍由你产出。

列义：`order` 订单类型（limit 限价 / stop 止损单 / market 市价 / none 仅结构背景，不构成入场）；`dir` 方向（bull / bear / ctx 读条件方向参数 / reverse 取反，依语境反转）；`风格` scalp 剥头皮 / swing 波段 / ctx 视通道宽度；`信号K` 该序列的信号棒定位；`入场K` 入场单挂法。

| sequence | order | dir | 风格 | 信号K | 入场K |
|---|---|---|---|---|---|
| ii | stop | ctx | scalp | 母棒 | 母棒极值±1tick（ii 是突破棒前身） |
| iii | stop | ctx | scalp | 母棒 | 母棒极值±1tick |
| ioi | stop | ctx | scalp | 第二内包棒 | 母棒极值±1tick |
| oo | stop | ctx | scalp | 外包棒 | 外包棒极值±1tick（波动扩张） |
| two_bar_reversal | stop | reverse | scalp | 反转第二棒 | 第二棒极值外 1tick |
| mdb | limit | bull | swing | 第二底反转棒 | 区间下边界限价 |
| mdt | limit | bear | swing | 第二顶反转棒 | 区间上边界限价 |
| gap_bar | none | ctx | — | 末棒 | 趋势背景（均线缺口确认强度） |
| follow_through | market | ctx | scalp | 跟进棒 | 跟进棒收盘市价确认 |
| hl | none | bull | — | 最近 L 摆点 | 趋势背景（低点抬高） |
| lh | none | bear | — | 最近 H 摆点 | 趋势背景（高点降低） |
| climax_exhaustion | limit | reverse | swing | 高潮末棒 | 耗尽段反向限价（买低卖高） |
| dbl_top | limit | bear | swing | 第二顶摆点棒 | DB 位限价（顶回落） |
| dbl_bot | limit | bull | swing | 第二底摆点棒 | DB 位限价（底回升） |
| hs_top | stop | bear | swing | 右肩 H 摆点棒 | 跌破颈线 sell stop |
| hs_bot | stop | bull | swing | 右肩 L 摆点棒 | 突破颈线 buy stop |
| wedge | stop | reverse | swing | 第三推摆点棒 | 第三推反向突破 1tick |
| shrinking_stairs | stop | reverse | swing | 末摆点棒 | 阶梯末段反向突破 |
| climax_reversal | stop | reverse | swing | 反向确认棒（末棒） | 反向棒极值外 1tick |
| high1 | limit | bull | ctx | 第1个递升 L 摆点棒（H1 信号K） | EMA/回调位 buy limit（通道回调限价口径） |
| high2 | limit | bull | ctx | 第2个递升 L 摆点棒（H2 信号K） | EMA/回调位 buy limit |
| high3 | limit | bull | ctx | 第3个递升 L 摆点棒（H3 楔形牛旗末端） | EMA/三推末端 buy limit |
| low1 | limit | bear | ctx | 第1个递降 H 摆点棒（L1 信号K） | EMA/回调位 sell limit |
| low2 | limit | bear | ctx | 第2个递降 H 摆点棒（L2 信号K） | EMA/回调位 sell limit |
| low3 | limit | bear | ctx | 第3个递降 H 摆点棒（L3 楔形熊旗末端） | EMA/三推末端 sell limit |
| retrace_shallow | limit | ctx | swing | 回调末端摆点棒 | 浅回调斐波位限价 |
| retrace_normal | limit | ctx | swing | 回调末端摆点棒 | 0.382-0.5 斐波位限价 |
| retrace_deep | limit | ctx | swing | 回调末端摆点棒 | 0.5-0.618 斐波位限价 |
| retrace_over | none | ctx | — | 回调末端摆点棒 | 过深回调：放弃（趋势结构存疑） |
| pullback_L0 | limit | ctx | swing | 回调末端摆点棒 | 回调 ≤1 根限价顺势 |
| pullback_L1 | limit | ctx | swing | 回调末端摆点棒 | 回调 ≤2 根限价顺势 |
| pullback_L2 | limit | ctx | swing | 回调末端摆点棒 | 回调 ≤5 根限价顺势 |
| pullback_L3 | limit | ctx | swing | 回调末端摆点棒 | 回调 ≤10 根限价顺势 |
| pullback_L4 | limit | ctx | swing | 回调末端摆点棒 | 回调 ≤20 根限价顺势 |
| pullback_L5 | none | ctx | — | 回调末端摆点棒 | 回调 >20 根：放弃（20根法则） |
| two_leg | limit | ctx | swing | C 腿末端摆点棒 | 两腿回调末端限价 |
| breakout_ignition | stop | ctx | scalp | 引爆突破棒 | 突破棒极值外 1tick buy/sell stop |
| breakout_quality | stop | ctx | scalp | 突破棒 | 真突破跟随止损单 |
| breakout_pullback | stop | ctx | swing | 回踩测试棒 | 回撤后再突破：极值外 1tick 止损单 |
| pbt | stop | ctx | swing | 突破测试棒 | 测试后再突破：极值外 1tick 止损单 |
| channel_class | none | ctx | — | 末棒 | 通道分类背景（回调入场语境） |
| channel_overshoot | none | ctx | — | 末棒 | 过冲计数背景（回归预警） |
| range_position | none | ctx | — | 末棒 | 区间位置背景（限价族语境） |
| tight_range_duration | none | ctx | — | 末棒 | 紧区间背景（突破止损单准备） |
| gap_class | none | ctx | — | 末棒 | 缺口分类背景 |
| gap_strength | none | ctx | — | 末棒 | 缺口强度背景 |
| magnet_rank | none | ctx | — | 末棒 | 磁铁排序背景（目标位语境） |
| vacuum | none | ctx | — | 末棒 | 真空效应背景（强趋势确认） |
| tf_alignment | none | ctx | — | 末棒 | 多周期对齐背景 |
| opening_range | stop | ctx | scalp | 开盘区间突破棒 | 区间边界外 1tick 止损单 |
| trend_day | none | ctx | — | 末棒 | 趋势日类型背景 |
| dbl | stop | bear | swing | 反转熊棒（末棒） | 反转棒低点下方 sell stop（保守于强棒开盘） |
| dbs | stop | bull | swing | 反转牛棒（末棒） | 反转棒高点上方 buy stop（保守于强棒开盘） |
| failed_h1 | stop | bear | swing | 第1个递升 L 摆点棒（被穿信号K） | 信号K低点下方 sell stop（多头失败做空） |
| failed_h2 | stop | bear | swing | 第2个递升 L 摆点棒（被穿信号K） | 信号K低点下方 sell stop |
| failed_l1 | stop | bull | swing | 第1个递降 H 摆点棒（被穿信号K） | 信号K高点上方 buy stop（空头失败做多） |
| failed_l2 | stop | bull | swing | 第2个递降 H 摆点棒（被穿信号K） | 信号K高点上方 buy stop |
| final_flag | stop | reverse | swing | 旗体末棒 | 旗体反向突破 1tick（M40：突破常失败反转） |
| give_up_bar | stop | reverse | swing | surprise 放弃棒（末棒） | 放弃棒反向极值外 1tick |
| expanding_triangle | stop | reverse | swing | 最近摆点棒（扩张末端） | 边界突破 1tick（ET 即 TR，强突破方向） |
| failed_failure | stop | ctx | swing | 恢复趋势棒（=BOP 第二信号） | 信号K极值外 1tick（原趋势恢复） |
| magb | stop | ctx | swing | 均线缺口末棒 | 末棒极值外 1tick（20根法则顺势） |
| trend_line_breakout | stop | ctx | swing | 趋势线突破棒（末棒） | 线值外 1tick（bear=升线跌破/多头切空） |

**读法**：`order=none` 的 20 条是**结构背景类**，只能作 `rule_ids` 的依据或目标位语境，**不能单独构成入场触发**；`dir=ctx` 表示方向由条件参数给定，`reverse` 表示取条件方向的反向。突破族（`breakout_*`/`pbt`/`trend_line_breakout`）统一 **stop**，不得写 limit。

共 63 条。
