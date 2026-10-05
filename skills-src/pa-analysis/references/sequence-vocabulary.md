# 信号序列词表（63 条）

> **形态类条件里的 sequence 名必须取自本表**（与执行侧枚举逐字一致），写错会被拒。
> `min_bars` 是该序列判定所需的**最少已收盘 K 根数** —— 数据不足时不要引用该序列（程序会判 manual）。判据均基于已收盘K，盘中不翻转。

| sequence | min_bars | 判据注记 |
|---|---|---|
| breakout_ignition | 21 | 引爆点（需前 20 根均实体/均量基线） |
| channel_class | 20 | 通道四分类（窗口） |
| channel_overshoot | 20 | 通道过冲计数（窗口 + 摆点轨线） |
| gap_class | 20 | 缺口四分类（窗口） |
| magb | 20 | 均线缺口K（连续 20 根未触 EMA20） |
| magnet_rank | 20 | 磁铁系统排序表（窗口 + 摆点） |
| range_position | 20 | 区间磁性位置（窗口内相对位） |
| tight_range_duration | 20 | 紧区间持续根数（窗口内连续计数） |
| trend_day | 20 | 趋势日类型（窗口聚合判型） |
| opening_range | 16 | 开盘区间（前 15 根成区间 + 1 根突破判定K） |
| final_flag | 15 | 最终旗形（趋势段 ≥5 + 旗体 ≥10） |
| expanding_triangle | 12 | 扩张三角形（3H 逐升 ∧ 3L 逐降） |
| failed_failure | 12 | 失败之失败（=BOP 变体，原趋势恢复） |
| high3 | 12 | H3 第三次多头回调（楔形旗形多头端） |
| hs_bot | 12 | 头肩底（需 ≥5 摆点：三峰 + 两谷） |
| hs_top | 12 | 头肩顶（需 ≥5 摆点：三峰 + 两谷） |
| low3 | 12 | L3 第三次空头回调（楔形旗形空头端） |
| failed_h1 | 10 | 失败 H1（多头信号触发后反向穿信号棒低点） |
| failed_h2 | 10 | 失败 H2 |
| failed_l1 | 10 | 失败 L1（空头信号触发后反向穿信号棒高点） |
| failed_l2 | 10 | 失败 L2 |
| trend_line_breakout | 10 | 趋势线突破（两同向摆点连线被收盘突破） |
| wedge | 10 | 楔形三推（3 同向摆点 + 缓冲） |
| dbl_bot | 8 | 双底（2×SWING_ORDER+2 保证摆点） |
| dbl_top | 8 | 双顶（同上） |
| high1 | 8 | H1 多头回调信号（2×SWING_ORDER+2 保证摆点） |
| high2 | 8 | H2 二次多头回调信号 |
| hl | 8 | 摆动低点抬高（2×SWING_ORDER+2） |
| lh | 8 | 摆动高点降低 |
| low1 | 8 | L1 空头回调信号 |
| low2 | 8 | L2 二次空头回调信号 |
| pullback_L0 | 8 | 回调K计数档：连续逆势K ≤1 根 |
| pullback_L1 | 8 | 回调K计数档：≤2 根 |
| pullback_L2 | 8 | 回调K计数档：≤5 根 |
| pullback_L3 | 8 | 回调K计数档：≤10 根 |
| pullback_L4 | 8 | 回调K计数档：≤20 根 |
| pullback_L5 | 8 | 回调K计数档：>20 根（20 根法则） |
| retrace_deep | 8 | 回调深度深档（0.5~0.618 腿幅） |
| retrace_normal | 8 | 回调深度正常档（0.382~0.5） |
| retrace_over | 8 | 回调深度过深档（>0.618） |
| retrace_shallow | 8 | 回调深度浅档（<0.382） |
| two_leg | 8 | 两腿回调 ABC（末端摆点三段） |
| breakout_pullback | 6 | 突破回撤分档 |
| breakout_quality | 6 | 真/假突破（回看 5 根 + ≥1 根跟随） |
| shrinking_stairs | 6 | 收缩阶梯（3 段推动） |
| climax_reversal | 4 | 高潮反转（高潮腿 ≥3 + 末根反向） |
| dbl | 4 | 失望多头（强棒同向二次测试失败，空头信号） |
| dbs | 4 | 失望空头（镜像，多头信号） |
| iii | 4 | 连续 3 根内包 |
| ioi | 4 | 内-外-内 三棒组合（首内包需对照前K） |
| pbt | 4 | 突破测试（结构不足时不成立） |
| climax_exhaustion | 3 | 高潮腿耗尽（末段 ≥3 根大实体） |
| give_up_bar | 3 | 放弃K GUB（surprise bar 大实体大波幅） |
| ii | 3 | 连续 2 根内包（2 组包含关系需 3 根K） |
| oo | 3 | 连续两根外包（首外包需对照前K） |
| vacuum | 3 | 真空效应（连续 ≥3 根大K） |
| follow_through | 2 | 跟进棒（最新棒 + 前 1-2 根对照） |
| gap_strength | 2 | 缺口强度（单棒 vs 前棒） |
| mdb | 2 | 微双底（相邻两K低点接近） |
| mdt | 2 | 微双顶（相邻两K高点接近） |
| two_bar_reversal | 2 | 双棒反转 |
| gap_bar | 1 | 均线缺口棒（单棒 vs EMA20） |
| tf_alignment | 1 | 时间框架对齐（跨周期分支，不走单周期 min_n） |

**用法**：`sequence` 条件必须同时给 `params.sequence`（上表名）+ 方向/周期等参数；`min_bars` 不足 → 该条件判 manual（等于没触发）。`signal_bar` 形态名另见 `contract-enums.md` 的 11 种枚举。

共 63 条。
