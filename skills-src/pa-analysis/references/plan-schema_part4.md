> 本文件是 `plan-schema_part1.md` 的第 4/6 片（按 `##` 小节切分，内容未改动）。

### sequence 条件枚举（63 种，type=sequence）

| sequence | 判定语义 |
|---|---|
| `ii` | 连续 2 根内包（iii 蕴含 ii） |
| `iii` | 连续 3 根内包 |
| `ioi` | 内-外-内三棒组合（波动收缩→选择方向→再收缩） |
| `oo` | 连续两根外包且第二根严格扩张（high 更高 ∧ low 更低） |
| `two_bar_reversal` | 双棒反转：前根反向强棒 + 末根同向棒实体≥前根 50%；`variant` ∈ standard/engulfing/flat（默认 standard） |
| `mdb` | 微双底：相邻两K低点差 ≤ `tolerance_atr`×ATR（默认 0.02）+ 第二根收阳 + 收盘远高于低点(close_pos≥0.5) |
| `mdt` | 微双顶：相邻两K高点差 ≤ `tolerance_atr`×ATR（默认 0.02）+ 第二根收阴 + 收盘远低于高点(close_pos≤0.5) |
| `gap_bar` | 均线缺口棒（**非跳空缺口**，出处文件16 五）：多头背景整根K在 EMA20 下方（high<ema20），空头镜像（low>ema20） |
| `follow_through` | 坚持到底棒（theme15 15.1 前瞻式）：最新已收盘K 同向（bull 收阳/bear 收阴）+ 收盘相对前棒同向推进 |
| `hl` | 最近摆动低点更高（swing order=3，多头控制） |
| `lh` | 最近摆动高点更低（swing order=3，空头控制） |
| `climax_exhaustion` | 高潮腿耗尽：末段≥3 根连续同向大实体严格递增（延续耗尽迹象，非反转确认） |
| `dbl_top` | 双顶（M39）：最近两 H 摆点价差 ≤0.02×ATR（ATR 缺失回退相对差 2%）∧ 最新收盘 < 两峰间最低 low（颈线）；bear 结构（序列名自带方向） |
| `dbl_bot` | 双底（M39）：最近两 L 摆点价差 ≤0.02×ATR ∧ 最新收盘 > 两谷间最高 high（颈线）；bull 结构（序列名自带方向） |
| `hs_top` | 头肩顶（M15/M27）：最近 3 个 H 摆点中峰（头）最高、右肩 ≤ 左肩×(1+3%) ∧ 最新收盘 < 两肩间 L 摆点较高者（颈线）；bear 结构 |
| `hs_bot` | 头肩底（M15/M27）：最近 3 个 L 摆点中谷（头）最低、右肩 ≥ 左肩×(1-3%) ∧ 最新收盘 > 两肩间 H 摆点较低者（颈线）；bull 结构 |
| `wedge` | 楔形三推（M15/M47）：三次同向推动单调推进且幅度递减（第三推 < 第二推×90%）——动能衰减，无方向 |
| `shrinking_stairs` | 收缩阶梯（M41）：最近 3 次同向推动高度严格递减（d3<d2<d1）——动能衰减，无方向 |
| `climax_reversal` | 高潮反转（M33）：末根前 climax_leg 成立（≥3 根同向大实体扩张）∧ 末根反向且实体≥0.70——反转确认，无方向 |
| `high1` | H1 多头回调信号（M36/M62/M59）：最近 H 摆点锚后首个 higher-low 形成；序列名自带方向（不读 direction）；摆点 <2 → manual |
| `high2` | H2 二次多头回调信号：锚后第二个 higher-low（h2 蕴含 h1）；同上自带方向 |
| `low1` | L1 空头回调信号：最近 L 摆点锚后首个 lower-high；序列名自带方向 |
| `low2` | L2 二次空头回调信号：锚后第二个 lower-high（l2 蕴含 l1）；自带方向 |
| `retrace_shallow` | 回调深度浅档（M36/M10/M28）：最近腿回撤比 <0.382；`direction` 可选限侧；摆点/腿幅不足 → manual |
| `retrace_normal` | 回调深度正常档：回撤比 ∈ [0.382, 0.5)；同上 |
| `retrace_deep` | 回调深度深档：回撤比 ∈ [0.5, 0.618]；同上 |
| `retrace_over` | 回调深度过深档：回撤比 >0.618；同上 |
| `pullback_L0` | 回调K计数档（M62/M28/M36）：连续逆势K ≤1 根；不读 direction；无趋势前提 → manual |
| `pullback_L1` | ≤2 根；同上 |
| `pullback_L2` | ≤5 根；同上 |
| `pullback_L3` | ≤10 根；同上 |
| `pullback_L4` | ≤20 根；同上 |
| `pullback_L5` | >20 根；同上 |
| `two_leg` | 两腿回调 ABC（M35/M36/M10）：末端三段摆点 L→H→L(higher-low) 或 H→L→H(lower-high) 镜像；`direction` 可选限侧；摆点 <3 → met=False（未出现≠数据不足） |
| `breakout_ignition` | 引爆点（M45/M33/M63）：末根实体 >3×前 20 根均实体；量列可得时再要求 >2×均量（量缺失仅实体弱化判）；`direction` 可选限侧；K线 <21 → manual |
| `breakout_quality` | 真/假突破（M45/M28/M59）：最近收盘破前摆点后回撤深度 <0.5 腿=true / ≤1.0=weak / >1.0 吞没=false；`expect`（默认 true）比对；无突破结构 → manual |
| `breakout_pullback` | 突破回撤分档（M45/M36/M10）：回撤 <0.25 腿=none / ≤0.50=shallow / >0.50=deep；`expect`（默认 shallow）比对；无突破结构 → manual |
| `pbt` | 突破测试 PBT（M56/M45/M03）：破前摆点后回踩该位 ±0.02×ATR（ATR 缺失按关键位 0.1% 兜底）∧ 最新收盘反弹离开；`direction` 可选限侧；结构不足 → met=False |
| `channel_class` | 通道四分类（M44/M31）：flat=窗口幅度<1×ATR；steep=每根净推进≥0.5×ATR；回撤≤3 根且深<2×ATR=narrow；其余 wide；摆动无序 → None；`expect` ∈ narrow/wide/steep/flat（必填）比对 |
| `channel_overshoot` | 通道过冲计数（M44/M31）：破摆点递升/递降轨线外延线 ±0.1×ATR 容差后 ≤5 根内收盘回归的次数(0-4封顶)；无通道结构 → None → manual；`expect:int`(精确)/`min:int`(下限) 至少其一 |
| `range_position` | 区间磁性位置（M46）：收盘在窗口区间相对位 <0.20=low / >0.80=high / 其余 mid；数据不足 → None；`expect` ∈ low/mid/high（必填）比对 |
| `tight_range_duration` | 紧区间持续根数（M42）：连续「range<2×ATR ∧ 重叠率≥0.80」根数（<10/≥10 分档上层解释）；ATR 缺失 → None；`min:int`/`max:int`/`expect:int` 至少其一 |
| `gap_class` | 缺口四分类（M09/M29/M37）：\|open−prev_close\|≥0.1×ATR 才算缺口；breakout=前窗口趋势≥0.5×ATR ∧ 收盘越窗口极值≥0.1×ATR；measured=趋势成立不破极值；exhaustion=前窗口累计净推进≥2×ATR；common=其余；无有效缺口 → None → manual；`expect` ∈ breakout/measured/exhaustion/common（必填）比对 |
| `gap_strength` | 缺口强度（M09/M29）：strong=影线间缺口 / mid=实体间 / weak=仅影线重叠；无缺口 → None → manual；`expect` ∈ strong/mid/weak（必填）比对 |
| `magnet_rank` | 磁铁系统（M37/M10/M22）：磁体排序表（swing_high/swing_low/ema20/round/mm_up/mm_down 按距现价升序）；`expect_kind` ∈ 上列（必填）+ 可选 `max_dist_atr:num`（最近该型磁体距现价 ≤N×ATR）；只做存在性/邻近度查询不下方向结论；数据不足 → manual |
| `vacuum` | 真空效应（M37）：连续 ≥3 根 range≥1.5×ATR 同向大K 无逆向回撤急奔磁铁；无 params；结构 bool，未出现 → False（不 manual） |
| `tf_alignment` | 时间框架对齐（12_multi_timeframe.md）：HTF/MTF/LTF 三层方向 → 对齐等级（3aligned/2aligned/mixed/conflict）；跨周期输入由 `htf`/`mtf`/`ltf`（三层 interval 键，至少 htf 必填）指定，每层状态取 ctx.always_in_states → 程序化自算兜底；`expect` ∈ 3aligned/2aligned/mixed/conflict（必填）；HTF 状态不可得或三层均无方向 → manual |
| `opening_range` | 开盘区间（M26/M57）：前 n 根（默认 15，`n` 可覆盖）成开盘区间，第 n+1 根收盘判定 up_break（收盘>区间高点）/down_break（<区间低点）/inside；`expect` ∈ up_break/down_break/inside（必填）比对；当日会话K不足 n+1 根或窗口未覆盖会话起点 → manual（真实开盘棒不可还原，绝不猜） |
| `trend_day` | 趋势日类型（M57）：窗口聚合判型 strong_trend（趋势日）/gap_trend（跳空趋势日）/spike_channel（尖峰通道日）；`expect` ∈ strong_trend/gap_trend/spike_channel（必填）比对；判不出三型 → manual（其余类型归 AI 判读） |
| `dbl` | 失望多头（M39，批8）：窗口内最近强牛棒（实体≥70%）后某棒再测其高点（微双顶，容差 0.02×ATR）但未守住，最新棒熊K收盘<强棒开盘（多头被套）→ 空头信号；序列名自带方向（同 mdb/mdt 口径，不读 direction）；结构未出现 → False |
| `dbs` | 失望空头（M39，批8）：强熊棒+微双底+最新牛K收盘>强棒开盘 → 多头信号；镜像，自带方向 |
| `high3` | H3 第三次多头回调（M51 规则10/M62 §15，批8）：与 h2_l2_count 完全同源的连击步数，n≥3 → met；H3 通常是楔形牛旗末端（三推递减）；自带方向；摆点 <2 → manual |
| `low3` | L3 第三次空头回调（=楔形熊旗末端，批8）：镜像；摆点 <2 → manual |
| `failed_h1` | 失败 H1（M59，批8）：H1 信号棒（_nth_signal_bar 同源定位）触发（某棒 high>信号棒 high）∧ 最新收盘<信号棒 low（反向穿越，多头止损被触发）→ 空头预警；自带方向；与 momentum_check `failed_h12` 分工——彼查「触发后涨幅不足 r×R 即回落」，此查「反向穿另一极值」；摆点 <2 → manual，信号棒不存在/未失败 → False |
| `failed_h2` | 失败 H2（批8）：第二次回调信号失败，同上口径 |
| `failed_l1` | 失败 L1（M59，批8）：L1 信号棒触发后最新收盘>信号棒 high → 多头预警；镜像 |
| `failed_l2` | 失败 L2（批8）：镜像 |
| `final_flag` | 最终旗形（M40/M51 规则15，批8）：前置趋势段（≥5 根收盘 ≥70% 在 EMA20 同侧且 EMA 同向）+ 末端 ≥10 根窄幅旗体（范围 ≤2×ATR）；M40：突破通常失败并反转；语境方向交 AI/上层判读（不读 direction）；ATR/EMA 不可得 → manual |
| `give_up_bar` | 放弃K GUB（M51 规则27，surprise bar 族，批8）：大实体（≥60%）∧大波幅（≥1.5×ATR）；反转语义无方向——bear_gub（大熊棒=多头放弃）/bull_gub（大牛棒=空头放弃）任一出现即 met；`expect` ∈ bear_gub/bull_gub（可选限型，非法值 → manual）；ATR 不可得 → manual |
| `expanding_triangle` | 扩张三角形（M51 规则25，批8）：最近 3 个 H 摆点逐个抬高 ∧ 3 个 L 摆点逐个降低（高低点同步扩张）；纯几何无方向（Brooks：ET 是完整 TR，强突破方向入场，突破方向交 AI/上层）；摆点不足/未扩张 → False |
| `failed_failure` | 失败之失败（M59/M51 规则26，=BOP 变体第二信号，批8）：回调信号失败后该失败本身也失败 → 原趋势恢复（高概率顺势）；读 `direction`（回退链）：bull=检测多头侧趋势恢复；信号棒缺失/未成立 → False |
| `magb` | 均线缺口K MAGB（M51 规则22，20 根法则，批8）：最近 20 根（含末棒）全部未触 EMA20（逐棒用自身时刻 ema20 列比较，缺失回退统一标量）；读 `direction`（回退链）：bull=bull_magb（20 根全在 EMA 上方）/bear=bear_magb；EMA 不可得/未达 20 根/有触碰 → manual/False |
| `trend_line_breakout` | 趋势线突破（M51 规则11，批8）：两个递升 L 摆点连成上升趋势线，最新收盘<线值（外推）→ bear（升势线被跌破）；两个递降 H 摆点镜像 → bull；读 `direction`（回退链）；摆点不足/无线可连/未突破 → False |

**sequence params**：`sequence`（必填名，上表 63 种）、`direction`（bull/bear；ii/iii/ioi/oo/hl/lh/dbl_*/hs_*/wedge/shrinking_stairs/climax_reversal/high1/high2/low1/low2/pullback_L0..L5 纯几何或序列名自带方向可不带（批2 high*/low*/pullback_L* 程序不读 direction），retrace_*/two_leg/breakout_ignition/breakout_quality/breakout_pullback/pbt 读 direction 做可选限侧（不填不限），two_bar_reversal/gap_bar/follow_through/climax_exhaustion 需方向可解析；批8 dbl/dbs/high3/low3/failed_h*/failed_l* 序列名自带方向，final_flag/give_up_bar/expanding_triangle 纯几何或语境方向不读，failed_failure/magb/trend_line_breakout 读 direction 做可选限侧）、`timeframe`（可选）、`tolerance_atr`（默认 0.02，mdb/mdt 用）、`variant`（two_bar_reversal 变体，默认 standard）、`expect`（breakout_quality ∈ true/weak/false 默认 true；breakout_pullback ∈ none/shallow/deep 默认 shallow；channel_class ∈ narrow/wide/steep/flat 必填；range_position ∈ low/mid/high 必填；gap_class ∈ breakout/measured/exhaustion/common 必填；gap_strength ∈ strong/mid/weak 必填；tf_alignment ∈ 3aligned/2aligned/mixed/conflict 必填——非法值 → manual）、`min`/`max`（channel_overshoot/tight_range_duration 至少给其一，int）、`expect_kind`（magnet_rank ∈ swing_high/swing_low/ema20/round/mm_up/mm_down 必填）/`max_dist_atr`（magnet_rank 可选 num）、`htf`/`mtf`/`ltf`（tf_alignment 三层 interval 键，至少 htf 必填，每层状态取 ctx.always_in_states → 程序化自算兜底）、`n`（opening_range 开盘区间根数，默认 15）。**批1 反转结构族（dbl_*/hs_*/wedge/shrinking_stairs/climax_reversal）阈值=程序常量**（内联于上表各条目：0.02×ATR 摆点容差 / 3% 肩容差 / ×0.90 推幅递减 / 3 段递减 / 反向实体≥0.70）；params 覆盖词（`neck_break`/`decrease_min`/`min_leg_atr`/`body_ratio_min` 及 dbl_* 的 `tolerance_atr` 扩展）**预留未启用**——计划写入将被程序忽略，调整阈值须改 特征口径 头部常量并走五步契约全同步。**批2 趋势回调族（high*/low*/retrace_*/pullback_L*/two_leg）阈值=程序常量**（0.382/0.5/0.618 回调深度分档、PULLBACK_BINS 回调K计数分档 (1,2,5,10,20)、swing order=3 摆点）；批2 无新覆盖词——momentum_check 扩展词 `max_consecutive_bars`/`failed_h12`/`r_factor`（默认 1.0）为实际消费词，见条件 params 字段表。**批3 突破族（breakout_ignition/breakout_quality/breakout_pullback/pbt）阈值=程序常量**（IGNITION_BODY_MULT=3.0 / IGNITION_VOL_MULT=2.0 / IGNITION_AVG_WINDOW=20；BO_QUALITY_TRUE=0.5 / BO_QUALITY_WEAK=1.0；BO_PB_NONE=0.25 / BO_PB_SHALLOW=0.50；PBT_TICK_ATR=0.02 / PBT_TICK_PCT=0.001）；批3 无新覆盖词——`expect`（true/weak/false、none/shallow/deep）为实际消费词。**批4 通道区间族（channel_class/channel_overshoot/range_position/tight_range_duration）阈值=程序常量**（CHANNEL_LOOKBACK=20 / CHANNEL_WIDTH_ATR=2.0 / CHANNEL_PULLBACK_NARROW=3 / CHANNEL_STEEP_ATR=0.5 / CHANNEL_FLAT_ATR=1.0 / CHANNEL_DIR_MIN_ATR=0.25 / OVERSHOOT_RETURN_N=5 / OVERSHOOT_TOL_ATR=0.1 / OVERSHOOT_COUNT_MAX=4 / RANGE_EDGE_PCT=0.20 / TIGHT_RANGE_N=10）；批4 无 params 覆盖词——`expect`（narrow/wide/steep/flat、low/mid/high）、`min`/`max` 为实际消费词。**铁丝网以新增 condition type `market_state` 落地**（`params.expect` ∈ breakout/narrow_channel/wide_channel/range/barb_wire/neutral，铁丝网=重叠率≥80%∧震荡K≥60%∧均幅<2×ATR 程序硬判）。**批5 缺口磁铁族（gap_class/gap_strength/magnet_rank/vacuum）阈值=程序常量**（GAP_LOOKBACK=20 / GAP_MIN_ATR=0.1 / GAP_TREND_MIN_ATR=0.5 / GAP_EXHAUST_LEG_ATR=2.0 / GAP_BREAK_TOL_ATR=0.1 / VACUUM_BAR_MIN_ATR=1.5 / VACUUM_RUN_N=3）；批5 无 params 覆盖词——`expect`（breakout/measured/exhaustion/common、strong/mid/weak）、`expect_kind`/`max_dist_atr` 为实际消费词；`vacuum` 为无参结构 bool。**批6 多周期族（tf_alignment）无阈值常量**（对齐等级判定优先级内联于 features.tf_alignment，跨周期输入经 ctx.candles 多键 + ctx.always_in_states），批6 无 params 覆盖词——`expect`（3aligned/2aligned/mixed/conflict）、`htf`/`mtf`/`ltf`（三层 interval 键）为实际消费词；批6 第 2 项「90 棒 EMA 替代」属节点1 数据源扩展，不进引擎词表。**批7 开盘日内族（opening_range/trend_day）**：`expect`（up_break/down_break/inside、strong_trend/gap_trend/spike_channel 必填）+ `n`（仅 opening_range，默认 15）为实际消费词。**批8 反转末端/回调失败补充族（dbl/dbs/high3/low3/failed_h1/h2/l1/l2/final_flag/give_up_bar/expanding_triangle/failed_failure/magb/trend_line_breakout）阈值=程序常量**（复用既有同源口径：dbl/dbs 强棒实体≥0.70+微双顶容差 0.02×ATR、h3/l3 与 h2_l2 同源连击、GUB 实体≥0.60∧波幅≥1.5×ATR、final_flag 趋势段≥5 根同侧占比≥0.70+旗体≥10 根≤2×ATR、MAGB 20 根未触 EMA20、摆点 order=3）；批8 params 覆盖词仅 `expect`（give_up_bar ∈ bear_gub/bull_gub，可选限型），其余批8 名无附加 params；**交易意图元数据**（订单类型/方向/剥头皮vs波段/信号K/入场K 定位规则）见 `执行侧交易意图表` `TRADE_INTENT`（63 条与词表双向对齐，节点2 撰写计划时的意图参考，非门禁词表）。

**语义注记**：
- **gap_bar = 均线缺口棒，不是跳空缺口**：多头/横盘背景的缺口棒指整根K位于 EMA20 下方（high<ema20——趋势已出现足够深的回撤、测试均线另一侧空间），空头背景镜像。出处：文件16 五。
- **mdb/mdt（微双底/微双顶）**：相邻两K低点/高点差 ≤ tolerance_atr×ATR（默认 0.02×ATR）**且第二根收阳/收阴、收盘远高于低点/低于高点（文件16 六「好 MDB/MDT」关键条件，防「极值接近但第二根收在低/高点附近继续下压」误判）**。
- **mdb/mdt vs dbl/dbs（批8，勿混淆）**：知识库有两对相近概念——①文件16 六「微双底/微双顶」= 相邻两K极值接近（程序词表 `mdb`/`mdt`）；②39_dbl_dbs.md「失望多头 DBL/失望空头 DBS」= 强棒同向二次测试失败后反转（交易者心理结构，程序词表批8 新名 `dbl`/`dbs`，序列名自带方向：dbl=空头信号/dbs=多头信号）。历史注记「DBL↔mdt、DBS↔mdb 等价映射」系批8 落地前的近似归并，**自批8 起以独立词表名为准**。
- **two_bar_reversal（双棒反转）**：两根反向趋势棒，第二根实体≥第一根的 50%；前根「强棒」=实体占比≥70%（统一大实体口径，非文件16 相对均值）；variant ∈ standard/engulfing/flat——engulfing 吞没型=第二根整根高低点包络第一根（文件16 四.2b「完全包含，类似外包」）；flat 平底平顶型=两低点/高点接近 ≤0.02×ATR（文件16 六同 mdb/mdt 口径）。
- **climax_exhaustion（高潮耗尽）**：末段≥3 根连续同向大实体且实体向末端严格递增——延续耗尽迹象，met=True 表示出现耗尽迹象。
- **hl/lh**：最近摆动低点更高/高点更低（swing order=3）。
- 全部 sequence 判定仅依赖**已收盘K线**（盘中不翻转）；K线不足 / ATR·EMA 不可得 / 未知名 / 方向不可解析 → manual（fail-safe，绝不猜）。

