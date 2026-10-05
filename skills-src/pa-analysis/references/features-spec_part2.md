> 本文件是 `features-spec.md` 的第 2/2 片（按 `##` 小节切分，内容未改动）。

## 十一、趋势回调族原语（B类下沉批2，v6 新增）

**定位**：同 §十——窗口级序列判定原语，输入整段已收盘K（最新在尾部），输出枚举/布尔/计数/None。
供执行侧的 sequence 条件（high1/high2/low1/low2、retrace_shallow/normal/deep/over、
pullback_L0..L5、two_leg，词表见 plan-schema_part1.md）与 momentum_check 扩展（max_consecutive_bars、
failed_h12）消费，与执行侧原语 同名原语契约对拍
（test_cross_copy_contract 把关）。边界纪律同 §一：只出几何事实/计数事实，禁方向观点/质量评价
（h1/l1 等为结构计数命名，非交易建议；trend_bar_count 只报计数，"年轻/警惕"结论交上层门）。

| 原语 | 签名 → 返回 | 概念来源 | 判据摘要 |
|---|---|---|---|
| h2_l2_count | (candles, atr=None, order=SWING_ORDER) → {"h1","h2","l1","l2"}(0/1) 或 None | M36/M62/M59/M15 | 最近 H 摆点为锚，其后 L 摆点相对锚前最近 L 逐次抬高（higher-low）连续计数：首次→h1=1、第二次→h2=1（h2 蕴含 h1），未抬高即停；空头镜像（最近 L 锚 + H 摆点连续走低→l1/l2）。摆点 <2 → None |
| retrace_depth_class | (candles, atr=None, order=SWING_ORDER, direction=None) → "shallow"/"normal"/"deep"/"over"/None | M36/M10/M28 | 腿端=最近摆点（H=多头腿/L=空头腿），腿幅=腿端与其前最近异型摆点价差；回撤=腿端后最深反向极值距离；ratio=回撤/腿幅：<RETRACE_SHALLOW(0.382)→shallow / <RETRACE_MID(0.5)→normal / ≤RETRACE_DEEP(0.618)→deep / >0.618→over。direction 限侧不符 / 摆点·腿幅不足 → None |
| pullback_bar_count | (candles, order=SWING_ORDER) → "L0".."L5"/None | M62/M28/M36 | 趋势前提=last_swing_direction（hl=多头结构→逆势K=收盘下移K；lh 镜像）；自最新K向前数连续逆势K根数 n（持平即中断），按 PULLBACK_BINS 分档：n≤1→L0 / ≤2→L1 / ≤5→L2 / ≤10→L3 / ≤20→L4 / >20→L5。无趋势前提/K线<2 → None |
| trend_bar_count | (candles, direction=None) → int/None | M28/M20/M34「20根法则」 | 尾部连续同色K根数（持平K中断；direction 限侧时末根不符→0）。仅返回计数事实——"≈20 警惕/>20 罕见反转"结论交 momentum_check max_consecutive_bars 门落定。空序列 → None |
| two_leg_pullback | (candles, atr=None, order=SWING_ORDER, direction=None) → bool | M35/M36/M10 | 末端三段摆点构成两腿回撤：多头 L1→H_bounce→L2 且 L2≥L1（第二腿守住 higher-low）；空头镜像 H1→L→H2 且 H2≤H1。direction 限侧；摆点 <3 → False |
| failed_h12 | (candles, atr=None, r_factor=1.0, direction=None, order=SWING_ORDER) → bool | M59/M36/M15 | 复用 h2_l2 摆点定位最近回调信号棒：入场=信号棒极值（多头=high/空头=low）、止损=对侧极值、R=两者价差；已触发（后续极值越过入场）∧ 最大顺向幅度 < r_factor×R ∧ 最新收盘回落入场下方（多头）→ True（失败）；未触发 ≠ 失败 → False |

**缺数据语义**（与批1 统一）：凡无法确定 → None（上层转 manual，fail-safe 不猜）；可选 pattern
未命中 → False（met=False，程序判否）。h2_l2_count 返回计数 0 是可判定结果（≠数据不足）。
**params 覆盖预留未启用**：批2 词表 tolerance/r_factor 以外无新覆盖词——阈值按 §三头部常量
（RETRACE_*/PULLBACK_BINS）判定；调整须改常量并走五步契约全同步（feature-addition-contract.md）。

---

## 十二、突破族原语（B类下沉批3，v7 新增）

**定位**：同 §十/§十一——窗口级序列判定原语，输入整段已收盘K（最新在尾部），输出枚举/布尔/None。
供执行侧的 sequence 条件（breakout_ignition/breakout_quality/breakout_pullback/pbt，
词表见 plan-schema_part1.md）消费，与执行侧原语 同名原语契约对拍
（test_cross_copy_contract 把关）。边界纪律同 §一：只出几何事实，禁方向观点/质量评价
（"true/weak/false"为结构分类命名，非交易建议）。

| 原语 | 签名 → 返回 | 概念来源 | 判据摘要 |
|---|---|---|---|
| breakout_ignition | (candles, atr=None, direction=None) → bool | M45/M33/M63 | 末根实体 > IGNITION_BODY_MULT(3.0)×前 IGNITION_AVG_WINDOW(20) 根均实体；量列可得时再要求 > IGNITION_VOL_MULT(2.0)×均量（量缺失仅实体弱化判）；direction 限侧时末根方向不符 → False；K线 <21 → False |
| breakout_quality | (candles, atr=None, direction=None) → "true"/"weak"/"false"/None | M45/M28/M59 | 最近 BREAKOUT_LOOKBACK(5) 根内收盘破其前摆点（H=bull/L=bear）为突破K，腿幅=突破K收盘−被破位；其后最大反向回撤/腿幅：< BO_QUALITY_TRUE(0.5)→true / ≤ BO_QUALITY_WEAK(1.0)→weak / >1.0 吞没→false；无突破结构/腿幅≤0/观察期不足 → None |
| breakout_pullback_class | (candles, atr=None, direction=None) → "none"/"shallow"/"deep"/None | M45/M36/M10 | 同上定位突破K与回撤比：< BO_PB_NONE(0.25)→none / ≤ BO_PB_SHALLOW(0.50)→shallow / >0.50→deep；无突破结构 → None |
| pbt_test | (candles, atr=None, direction=None) → bool | M56/M45/M03 | 最近 BREAKOUT_LOOKBACK(5) 根内收盘破前摆点后，其后某根极值回踩被破位 ± PBT_TICK_ATR(0.02)×ATR（ATR 缺失按被破位 PBT_TICK_PCT(0.001) 兜底）∧ 最新收盘已反弹离开该带 → True；未触及/仍驻留带内 → False；结构不足 → False |

**缺数据语义**（与批1/批2 统一）：凡无法确定 → None（上层转 manual，fail-safe 不猜）；
结构类「出现了才 met」原语缺数据 → False。breakout_quality/breakout_pullback_class 的分类枚举
是可判定结果；breakout_ignition/pbt_test 返回 False 含「未出现」与「结构不足」两种（均 met=False）。
**params 覆盖预留未启用**：批3 词表 direction/expect 以外无新覆盖词——阈值按 §三头部常量
（IGNITION_*/BO_QUALITY_*/BO_PB_*/PBT_*）判定；调整须改常量并走五步契约全同步
（feature-addition-contract.md）。

---

## 十三、通道区间族原语（B类下沉批4，v8 新增）

**定位**：同 §十/§十一/§十二——窗口级序列判定原语，输入整段已收盘K（最新在尾部），返回枚举/计数/None。
供执行侧的 sequence 条件（channel_class/channel_overshoot/range_position/tight_range_duration，
词表见 plan-schema_part1.md）消费，与执行侧原语 同名原语契约对拍
（test_cross_copy_contract 把关）。边界纪律同 §一：只出几何事实/计数事实，禁方向观点/质量评价
（narrow/wide/steep/flat 为通道结构分类命名，非交易建议）。

| 原语 | 签名 → 返回 | 概念来源 | 判据摘要 |
|---|---|---|---|
| channel_classify | (candles, atr=None, lookback=CHANNEL_LOOKBACK) → "narrow"/"wide"/"steep"/"flat"/None | M44/M31 | 窗口幅度 < CHANNEL_FLAT_ATR(1.0)×ATR → flat；\|窗口净推进\| ≥ CHANNEL_DIR_MIN_ATR(0.25)×ATR 且每根净推进 ≥ CHANNEL_STEEP_ATR(0.5)×ATR → steep；回撤 ≤ CHANNEL_PULLBACK_NARROW(3) 根且深 < CHANNEL_WIDTH_ATR(2.0)×ATR → narrow；其余 → wide；摆动无序/ATR 不可得 → None |
| channel_overshoot | (candles, atr=None, lookback=CHANNEL_LOOKBACK, return_n=OVERSHOOT_RETURN_N) → int(0−OVERSHOOT_COUNT_MAX)/None | M44/M31 | 摆点递升(HH)/递降(LL)轨线外延线 ± OVERSHOOT_TOL_ATR(0.1)×ATR 越轨后 ≤ return_n(5) 根内收盘回归轨内 = 有效过冲，次数封顶 OVERSHOOT_COUNT_MAX(4)；无通道结构 → None |
| range_position | (candles, atr=None, lookback=CHANNEL_LOOKBACK) → "low"/"mid"/"high"/None | M46 | 窗口区间 [min_low, max_high]；收盘相对位 < RANGE_EDGE_PCT(0.20) → low / > 1−RANGE_EDGE_PCT → high / 其余 mid；区间幅度=0 或数据不足 → None |
| tight_range_duration | (candles, atr=None, lookback=CHANNEL_LOOKBACK) → int/None | M42 | 自末根向前连续「range<2×ATR ∧ 重叠率≥0.80」根数（分档 <TIGHT_RANGE_N(10) / ≥10 由上层解释）；ATR 不可得 → None |

**缺数据语义**（与批1-批3 统一）：凡无法确定 → None（上层转 manual，fail-safe 不猜）；
channel_classify/range_position 的分类枚举与 channel_overshoot/tight_range_duration 的计数是可判定结果。
**params 覆盖预留未启用**：批4 词表 expect（narrow/wide/steep/flat、low/mid/high）、min/max 为实际消费词——
阈值按 §三头部常量（CHANNEL_*/OVERSHOOT_*/RANGE_EDGE_PCT/TIGHT_RANGE_N）判定；铁丝网走新增
condition type `market_state`（见 pattern-catalog_part1.md「批4 特殊项」，非本节 sequence 原语）；
调整须改常量并走五步契约全同步（feature-addition-contract.md）。

---

## 十四、缺口磁铁族原语（B类下沉批5，v9 新增）

**定位**：同 §十——窗口级序列判定原语，输入整段已收盘K（最新在尾部），返回枚举/布尔/排序表/None。
供执行侧的 sequence 条件（gap_class/gap_strength/magnet_rank/vacuum，词表见 plan-schema_part1.md）
消费，与执行侧原语 同名原语契约对拍（test_cross_copy_contract 把关）。
边界纪律同 §一：只出几何事实，禁方向观点/质量评价（breakout/measured/exhaustion/common、
strong/mid/weak 为缺口分类命名，非交易建议）。

| 原语 | 签名 → 返回 | 概念来源 | 判据摘要 |
|---|---|---|---|
| gap_classify | (candles, atr=None, lookback=GAP_LOOKBACK) → "breakout"/"measured"/"exhaustion"/"common"/None | M09/M29/M37 | \|open−prev_close\| ≥ GAP_MIN_ATR(0.1)×ATR 才算缺口；breakout=前窗口净推进 ≥ GAP_TREND_MIN_ATR(0.5)×ATR ∧ 收盘越窗口极值 ≥ GAP_BREAK_TOL_ATR(0.1)×ATR；measured=趋势成立不破极值；exhaustion=前窗口累计净推进 ≥ GAP_EXHAUST_LEG_ATR(2.0)×ATR；common=其余；无有效缺口/数据不足 → None |
| gap_strength | (candles) → "strong"/"mid"/"weak"/None | M09/M29 | 影线间缺口（前一K影线与本根影线间仍留空）→ strong；实体间缺口 → mid；仅影线重叠（实体无空但影线不空）→ weak；无缺口 → None |
| magnet_rank | (candles, atr=None, lookback=GAP_LOOKBACK) → [{"kind","price","dist"}]/None | M37/M10/M22 | 磁体候选：swing_high/swing_low（摆点极值）、ema20（窗口末 EMA20）、round（整数关口）、mm_up/mm_down（测量移动腿幅外推）；按距现价(dist)升序排序；无磁体/数据不足 → None |
| vacuum_effect | (candles, atr=None, lookback=GAP_LOOKBACK) → bool | M37 | 连续 ≥ VACUUM_RUN_N(3) 根同向大K(range≥ VACUUM_BAR_MIN_ATR(1.5)×ATR) 无逆向回撤急奔磁铁 → True；未出现 → False |

**缺数据语义**（与批1-批4 统一）：凡无法确定 → None（上层转 manual，fail-safe 不猜）；
vacuum_effect 为结构类 bool，未出现 → False（met=False，程序判否）。
**params 覆盖预留未启用**：批5 词表 expect（breakout/measured/exhaustion/common、strong/mid/weak）、
expect_kind/max_dist_atr 为实际消费词（gap_class/gap_strength 比对 expect、magnet_rank 查存在性+邻近度）——
阈值按 §三头部常量（GAP_*/VACUUM_*）判定；`vacuum` 无参结构 bool；调整须改常量并走五步契约全同步
（feature-addition-contract.md）。

## 十五、多周期族原语（B类下沉批6，v10 新增）

**定位**：批6 多周期族。`tf_alignment` 为**跨周期对齐纯函数**——与批1-5 的
「单周期窗口序列原语」不同，它接受三层（HTF/MTF/LTF）已归一的方向 token
（long/short/bull/bear/neutral/空），返回对齐等级枚举，不做「该不该交易」结论
（等级为共振程度分级，方向限侧由上层 params.direction 表达）。供镜像
执行侧序列判定 的 sequence 条件 `tf_alignment`（词表见 plan-schema_part1.md）消费，
与执行侧原语 同名原语契约对拍
（test_cross_copy_contract 把关）。边界纪律同 §一：只出对齐等级，禁方向观点/
质量评价（conflict/3aligned 等为共振等级命名，非交易建议）。

| 原语 | 签名 → 返回 | 概念来源 | 判据摘要 |
|---|---|---|---|
| tf_alignment | (htf, mtf, ltf) → "3aligned"/"2aligned"/"mixed"/"conflict"/None | 12_multi_timeframe.md | htf·mtf 均有向且相反 → conflict；htf==mtf==ltf 均有向 → 3aligned；htf 有向 且 (mtf 中性 ∧ ltf==htf) 或 (mtf==htf ∧ ltf 无信号) → 2aligned；三层均无向 → None；其余（仅 LTF 信号/方向模糊）→ mixed |

**缺数据语义**：三层 token 全部无向 → None（上层转 manual，fail-safe 不猜）；
`_dir_sign` 归一（long/bull→+1、short/bear→-1、其余→0）。批6 无阈值常量
（等级判定优先级内联）；批6 第 2 项「90 棒 EMA 替代」属节点1 数据源扩展
（1m 90 棒 EMA ≈ 5m 20 棒），非引擎原语，不进本 spec（另行登记）。
