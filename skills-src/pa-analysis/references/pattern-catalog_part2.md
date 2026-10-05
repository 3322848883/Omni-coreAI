> 本文件是 `pattern-catalog_part1.md` 的第 2/2 片（按 `##` 小节切分，内容未改动）。

## 五、信号K词表 ↔ 节点3程序对照矩阵（align-signal-bar-coverage，46 项）

> 用途：节点2 产计划时把知识库形态映射到节点3 程序可判定的 pattern/sequence 名（`trigger_conditions.params`），实现"程序能识别的 → 参数化全自动；程序不能判定的 → AI 归口"。**「程序 pattern/sequence」列与节点3 `执行侧序列判定` 枚举及 `references/plan-schema_part1.md`「信号K params 词表规范」逐字一致**；未登记名程序一律 manual（转监控员 AI，无 AI 时计划过期）。params 写法见 plan-schema 词表规范；交付门禁第 34 项程序化把关。

### 第 0 组：K线基本面（多头K线/空头K线——一切信号K的载体）

| # | 知识库形态 | 来源 | 程序 pattern/sequence | 归属（程序/AI） |
|---|---|---|---|---|
| 0a | 多头K线 bull bar（最小标准：收盘>开盘 或 收盘>中点） | theme15 15.4 最小特征；M01 趋势K | signal_bar `bull_bar` | 程序 |
| 0b | 空头K线 bear bar（最小标准：收盘<开盘 或 收盘<中点） | theme15 15.5 最小特征；M01 趋势K | signal_bar `bear_bar` | 程序 |
| 0c | 多头/空头趋势K（强趋势棒，实体>近期均值1.5倍 顺势延续） | 文件16 三.1；theme15 15.2 延续信号 | signal_bar `trend_bar`（实体>近 lookback 根均值×1.5 + 收盘距极点<20%实体，direction 必填） | 程序 |
| 0d | 通用信号K（无具名形态，仅几何阈值组合） | theme15 15.1 规则 2/7「每一棒都是架构/信号棒」 | signal_bar 通用 params（pattern 缺省：body_ratio_min/close_position_min·max/max_range_atr/within_entry_zone/or_long_wick·or_lower_wick） | 程序 |

### 第 1 组：反转与序列形态

| # | 知识库形态 | 来源 | 程序 pattern/sequence | 归属（程序/AI） |
|---|---|---|---|---|
| 1 | 反转棒 bull/bear（min/best 特征） | theme15 15.4/15.5 | signal_bar `reversal_bar`（专属 params：tail_ratio_min 默认 0.33 / tail_ratio_max 默认 0.60 / reversal_prior_close / overlap_ratio_max；长影变体另见 `hammer`/`shooting_star`） | 程序 |
| 2 | 双棒反转 2BR（第二根实体≥第一根 50%；前根=实体占比≥70% 强棒；标准型/吞没型/平底平顶型三变体；胜率约 50-55%） | theme15 15.7；文件16 四 | sequence `two_bar_reversal`（variant ∈ standard/engulfing/flat） | 程序 |
| 3 | 三棒反转 | theme15 15.7 | —（二期） | 程序（二期） |
| 4 | 内包棒 inside | theme15 15.2 | signal_bar `inside` | 程序 |
| 5 | ii | theme15 15.7；03 | sequence `ii` | 程序 |
| 6 | iii | theme15 15.7；03 | sequence `iii` | 程序 |
| 7 | ioi | theme15 15.7；03 | sequence `ioi` | 程序 |
| 8 | 外包棒 outside（含 OD/OU 方向变体：外包+收盘<开盘=OD 偏看跌；外包+收盘>开盘=OU 偏看涨） | 文件16 二/补充一 | signal_bar `outside`（params.direction=bull→OU 收阳 / bear→OD 收阴；不填保持纯几何） | 程序 |
| 9 | oo（外包棒后跟更大外包棒） | theme15 15.2 | sequence `oo` | 程序 |
| 10 | 双重顶/底 | theme15 15.2；03；M39 | sequence `dbl_top`/`dbl_bot`（最近两同向摆点价差 ≤0.02×ATR ∧ 最新收盘破两峰/谷间极值=颈线，swing order=3） | 程序 |
| 11 | MDB/MDT 微双底/顶（两根连续K低点/高点相同或接近；第二根收反向方向且收盘远离极点；程序以 tolerance_atr≈0.02×ATR 等价实现） | 文件16 六；03 DBL/DBS | sequence `mdb`/`mdt`（**别名等价：DBL=微型双顶↔`mdt`、DBS=微型双底↔`mdb`**） | 程序 |
| 12 | 光头光脚棒 shaved | theme15 15.7 | signal_bar 通用 params（close_position_min=0.95 / close_position_max=0.05 近似表达） | 程序（文档化） |
| 13 | 失败的反转尝试 | theme15 15.2 | —（二期） | 程序（二期） |
| 14 | 失败的延续尝试 | theme15 15.2 | —（二期） | 程序（二期） |
| 15 | 趋势棒反向使用 / 耗尽棒 | theme15 15.7；33_climax | sequence `climax_exhaustion`（末段≥3 根连续同向大实体严格递增） | 程序 |
| 16 | 均线缺口棒 GB/MAGB（**特指整根K在 EMA20 另一侧，非跳空缺口**）+ 20GB（连续 20 根未触 EMA） | 文件16 五；03 缺口信号表 | sequence `gap_bar`（多头背景 high<ema20 / 空头镜像；20GB 连续段为 features 统计） | 程序 |
| 17 | 平底/平顶 flat top/bottom | 文件16 | —（二期） | 程序（二期） |
| 18 | H1/H2/L1/L2 回撤计数 | 03；62_bar_counting；M36/M59 | sequence `high1`/`high2`/`low1`/`low2`（swing order=3 摆点锚后 higher-low/lower-high 连续计数，h2/l2 蕴含 h1/l1；序列名自带方向，程序不读 direction） | 程序 |
| 19 | 更高低点/更低高点 HL/LH | theme15 15.2 | sequence `hl`/`lh`（swing order=3） | 程序 |
| 20 | 强趋势尖峰期暂停 | theme15 15.2 | —（程序不判，纯语境） | **AI 归口**（ai_judged:true） |
| 21 | 通道中的棒线 / 二次入场 | theme15 15.2；15_二次入场 | —（程序不判，纯语境） | **AI 归口**（ai_judged:true） |
| 22 | ii最终旗形 / 区间陷阱 / 信号K×市场状态语境 | theme15 15.7；32_signal_context_matrix | —（程序不判，纯语境） | **AI 归口**（ai_judged:true） |
| 23 | 微楔形 MW（3-4 棒三推快速收敛；勿与单根锤子/流星混淆） | 文件16 补充三；14_楔形 | —（二期） | 程序（二期） |
| 24 | 尖峰态专属信号：SPS 尖峰回撤信号 / 尖峰旗形顺向突破（依赖 spike_stage 状态，**禁 SCS 追高潮**） | 文件16 七 | —（程序不判，纯语境） | **AI 归口**（ai_judged:true） |
| 25 | 头肩顶/底（左肩-头-右肩 + 颈线，颈线突破入场） | M15/M27；形态表 | sequence `hs_top`/`hs_bot`（最近 3 同向摆点中峰/谷极值 + 肩容差 3% + 最新收盘破颈线） | 程序 |
| 26 | 楔形三推（三次同向推动动能衰减） | M15/M47；14_楔形 | sequence `wedge`（三推单调推进且幅度递减：第三推 < 第二推×90%，无方向） | 程序 |
| 27 | 收缩阶梯（回撤深度递减、突破减小） | M41 | sequence `shrinking_stairs`（最近 3 段同向推动高度严格递减 d3<d2<d1，无方向） | 程序 |
| 28 | 高潮反转（加速耗尽后反向确认） | M33/M52 | sequence `climax_reversal`（末根前 climax_leg≥3 根同向大实体扩张 ∧ 末根反向实体≥0.70，无方向） | 程序 |
| 29 | 回调深度分档（fib 浅/正常/深/过深） | M36/M10/M28 | sequence `retrace_shallow`/`retrace_normal`/`retrace_deep`/`retrace_over`（最近腿回撤比 <0.382 / <0.5 / ≤0.618 / >0.618；direction 可选限侧） | 程序 |
| 30 | 回调K计数 L0-L5（连续逆势K根数分档） | M62/M28/M36 | sequence `pullback_L0`..`pullback_L5`（≤1/≤2/≤5/≤10/≤20/>20 根；需趋势前提，程序不读 direction） | 程序 |
| 31 | 两腿回调 ABC（第二腿守住 higher-low/lower-high） | M35/M36/M10 | sequence `two_leg`（末端三段摆点 L→H→L 或 H→L→H 镜像；direction 可选限侧） | 程序 |
| 32 | 20 根法则（连续同向 ≈20 警惕 / >20 罕见反转） | M28/M20/M34 | momentum_check `max_consecutive_bars`（连续同向 > 阈值 → not_met 反向门；计数事实由 features.trend_bar_count 出） | 程序 |
| 33 | 失败的 H1/H2（信号触发后未达 1R 即回落） | M59/M36/M15 | momentum_check `failed_h12:true` + `r_factor`（默认 1.0；复用 h2_l2 摆点定位信号棒，触发 ∧ 最大顺向 < r_factor×R ∧ 收盘回落入场下方 → True） | 程序 |
| 34 | 引爆点（突破K实体与量双双爆炸式放大） | M45/M33/M63 | sequence `breakout_ignition`（末根实体 >3×前 20 根均实体；量列可得时再要求 >2×均量，量缺失仅实体弱化判；direction 可选限侧） | 程序 |
| 35 | 真/假突破（最近突破的回撤深度三分类） | M45/M28/M59 | sequence `breakout_quality` + `expect:true/weak/false`（收盘破前摆点后回撤 <0.5 腿=true / ≤1.0=weak / >1.0 吞没=false；无突破结构 → manual） | 程序 |
| 36 | 突破回撤分档（none/shallow/deep） | M45/M36/M10 | sequence `breakout_pullback` + `expect:none/shallow/deep`（回撤 <0.25 腿=none / ≤0.50=shallow / >0.50=deep；无突破结构 → manual） | 程序 |
| 37 | 突破测试 PBT（回踩关键位 ±2%ATR 并反弹离开） | M56/M45/M03 | sequence `pbt`（破前摆点后回踩该位 ±0.02×ATR ∧ 最新收盘反弹离开；ATR 缺失按关键位 0.1% 兜底；direction 可选限侧） | 程序 |
| 38 | 通道四分类（窄/宽/陡/平，通过宽度、回撤根数、斜率角） | M44/M31 | sequence `channel_class` + `expect:narrow/wide/steep/flat`（flat=窗口幅度<1×ATR；steep=每根净推进≥0.5×ATR；回撤≤3 根且深<2×ATR=narrow；其余 wide；摆动无序 → manual） | 程序 |
| 39 | 通道过冲（破通道线后 5 根内回归=有效过冲，计数分档 1-4） | M44/M31 | sequence `channel_overshoot` + `expect:int`(精确)/`min:int`(下限)（破摆点递升/递降轨线外延线 ±0.1×ATR 容差后 ≤5 根内收盘回归；无通道结构 → manual） | 程序 |
| 40 | 区间磁性位置（距上/下界 <20% 区间高为边缘带） | M46 | sequence `range_position` + `expect:low/mid/high`（收盘在窗口区间相对位 <0.20=low / >0.80=high / 其余 mid；数据不足 → manual） | 程序 |
| 41 | 紧区间持续时长（<10/≥10 根分档） | M42 | sequence `tight_range_duration` + `min:int`/`max:int`/`expect:int`（连续「range<2×ATR ∧ 重叠率≥0.80」根数；ATR 缺失 → manual） | 程序 |
| 42 | 缺口四分类（突破/测量/衰竭/普通） | M09/M29/M37 | sequence `gap_class` + `expect:breakout/measured/exhaustion/common`（\|open−prev_close\|≥0.1×ATR 才算缺口；突破=前窗口趋势≥0.5×ATR ∧ 收盘越窗口极值≥0.1×ATR；测量=趋势成立不破极值；衰竭=前窗口累计净推进≥2×ATR；普通=其余；无有效缺口 → manual） | 程序 |
| 43 | 缺口强度（强/中/弱） | M09/M29 | sequence `gap_strength` + `expect:strong/mid/weak`（影线间缺口=strong / 实体间=mid / 仅影线重叠=weak；无缺口 → manual） | 程序 |
| 44 | 磁铁系统（磁体排序：摆点高/摆点低/EMA20/整数关口/MM 上/MM 下） | M37/M10/M22 | sequence `magnet_rank` + `expect_kind:swing_high/swing_low/ema20/round/mm_up/mm_down`（必填）+ 可选 `max_dist_atr:num`（最近该型磁体距现价 ≤N×ATR）；只做存在性/邻近度事实查询不下方向结论；数据不足 → manual | 程序 |
| 45 | 真空效应（连续 ≥3 根同向大K无回撤急奔磁铁） | M37 | sequence `vacuum`（无 params；连续 ≥3 根 range≥1.5×ATR 同向大K 无逆向回撤 → True；结构类 bool，未出现=False 不 manual） | 程序 |
| 46 | 时间框架对齐（HTF/MTF/LTF 三层方向共振等级） | 12_multi_timeframe.md | sequence `tf_alignment` + `expect:3aligned/2aligned/mixed/conflict`（必填）+ `htf`/`mtf`/`ltf`（三层 interval 键，至少 htf 必填；三级共振=3aligned / HTF+LTF 或 HTF+MTF=2aligned / 仅 LTF 或方向模糊=mixed / HTF·MTF 逆势=conflict；HTF 状态不可得或三层无方向 → manual） | 程序 |

> **批4 特殊项**：铁丝网（#31/#13 引用的 M42/M46「重叠率≥80%∧震荡K≥60%∧均幅<2×ATR」）以**新增 condition type `market_state`** 落地（非 sequence），`params.expect` ∈ breakout/narrow_channel/wide_channel/range/barb_wire/neutral（英文别名归一至节点3 `features.market_state` 中文枚举）；在 `plan_loader.FORM_TYPES` 登记、`signal_eval.evaluate_trigger` 加分支——12 批中首个新增 condition type，作为「新增类型」契约样板。

> **注记**：① 序列词表中的 `follow_through`（坚持到底棒，theme15 15.1 前瞻式：最新已收盘K 同向 + 收盘相对前棒推进）对应交易链"跟进K"确认概念（theme15 / trade-lifecycle 触发确认链），属确认机制而非独立知识库形态名，故不占矩阵行号；② 信号K质量通用标准（好信号棒：收盘接近极点/实体大尾短/长度≤平均 1.5 倍——文件16 一.1）由 #0d 通用 params（close_position + body_ratio + max_range_atr≈1.5×ATR）表达；③ "信号K产生信号的必要条件=后续K线突破信号棒极点"由既有 stop_breakout 条件单机制实现；④ #13、#14、#17、#23 列二期，不阻塞一期验收；⑤ 批1 反转结构族（#10 升级 + #25-#28 新增）阈值为程序常量（内联于 plan-schema 词表各条目），params 覆盖词预留未启用——计划写入被忽略，调阈值须改 特征口径 头部常量并走五步契约全同步；⑥ 批2 趋势回调族（#18 升级 + #29-#33 新增）阈值同为程序常量（0.382/0.5/0.618 回调分档、PULLBACK_BINS (1,2,5,10,20)、swing order=3），批2 无 params 覆盖词——momentum_check 扩展词 `max_consecutive_bars`/`failed_h12`/`r_factor` 为实际消费词（#32/#33）；⑦ 批3 突破族（#34-#37 新增）阈值同为程序常量（IGNITION_BODY_MULT=3.0 / IGNITION_VOL_MULT=2.0 / IGNITION_AVG_WINDOW=20；BO_QUALITY_TRUE=0.5 / BO_QUALITY_WEAK=1.0；BO_PB_NONE=0.25 / BO_PB_SHALLOW=0.50；PBT_TICK_ATR=0.02 / PBT_TICK_PCT=0.001），批3 无 params 覆盖词——`expect`（true/weak/false、none/shallow/deep）为实际消费词（#35/#36）；⑧ 批4 通道区间族（#38-#41 新增）阈值同为程序常量（CHANNEL_LOOKBACK=20 / CHANNEL_WIDTH_ATR=2.0 / CHANNEL_PULLBACK_NARROW=3 / CHANNEL_STEEP_ATR=0.5 / CHANNEL_FLAT_ATR=1.0 / CHANNEL_DIR_MIN_ATR=0.25 / OVERSHOOT_RETURN_N=5 / OVERSHOOT_TOL_ATR=0.1 / OVERSHOOT_COUNT_MAX=4 / RANGE_EDGE_PCT=0.20 / TIGHT_RANGE_N=10），批4 无 params 覆盖词——`expect`（narrow/wide/steep/flat、low/mid/high）、`min`/`max` 为实际消费词（#38-#41）；铁丝网走新增 condition type `market_state`（见上「批4 特殊项」）。 ⑨ 批5 缺口磁铁族（#42-#45 新增）阈值同为程序常量（GAP_LOOKBACK=20 / GAP_MIN_ATR=0.1 / GAP_TREND_MIN_ATR=0.5 / GAP_EXHAUST_LEG_ATR=2.0 / GAP_BREAK_TOL_ATR=0.1 / VACUUM_BAR_MIN_ATR=1.5 / VACUUM_RUN_N=3），批5 无 params 覆盖词——`expect`（breakout/measured/exhaustion/common、strong/mid/weak）、`expect_kind`（swing_high/swing_low/ema20/round/mm_up/mm_down）、`max_dist_atr`（num）为实际消费词（#42-#44）；`vacuum` 为无参结构 bool（#45）。 ⑩ 批6 多周期族（#46 新增）原语 `tf_alignment` 为跨周期对齐纯函数（无阈值常量，等级判据优先级内联），params 消费词 `expect`（3aligned/2aligned/mixed/conflict）、`htf`/`mtf`/`ltf`（三层 interval 键）为实际消费词（#46）；批6 第 2 项「90 棒 EMA 替代」属节点1 数据源扩展（非引擎原语，另行登记，不进本矩阵）。

## 使用流程（四步）
1. **识别**：先判断脉络（急速/通道/区间/突破/反转），再命名形态（本表或 M51）。
2. **判定**：逐条核对识别证据（可复查参数：数 K、量距离、验跟进）。
3. **使用**：给出 入场方式（M51 规则）+ 止损（假设失效位）+ 目标（图上磁体/MM）+ 风格（M66）。
4. **失败降级**：按本表"失败降级"列执行；无法命名或证据不足 → 观察，不输出入场。
