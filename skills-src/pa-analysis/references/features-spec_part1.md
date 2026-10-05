> 本文件是「几何特征口径规范」的第 1/2 片（按 `##` 小节切分，内容未改动）。

# 几何特征口径规范（AI 自算对照）

> **bot 没有特征计算脚本** —— 本文件的价值是给出**每个特征列的公式与阈值锚点**，让你用 `klines` 取到 OHLCV 后**自己按公式算**，而不是猜。
> 纪律：**只算事实，不把数字当意见**；公式必须与知识库原概念一致。本文档是这两条纪律的落地契约。

## 一、边界纪律（三条，违反任何一条 = 违规）

1. **只计算，不判断**：本口径只输出几何事实、比率数字、事实性分类。
   禁止出现：方向观点、信号质量评价、可靠性星级、"强/弱"标签、买卖建议、市场状态结论
   （趋势/区间是 AI 的判断——程序只提供 AI 判断所需的数字）。
2. **概念一致性**：每个特征公式对照知识库原始定义实现。分两类处理：
   - 知识库给出**明确量化锚点**的概念 → 程序按锚点出事实分类
     （例：大实体=实体占比≥70%，M24；长影=影线>实体2倍，M24；收盘三等分，M24；MAGB=不触EMA20，M03）
   - 知识库只有**定性描述**的概念（"大致相同"、"几乎没有重叠"、"约等于"）→
     程序只输出原始数字，**阈值判断留给 AI**（例：相邻同向摆动差只给 ATR 倍数，"是否构成微双顶"由 AI 按 M39 判）
   - **概念适用范围**：比率锚点隐含"K线有可测波动幅度"——薄片K线（range<0.15×ATR14）
     上比率分类不适用，程序输出 None 并标 bar_quality=thin（v3 新增；见 §四）
3. **数据同源**：EMA20/ATR14 直接读节点1 kline 表字段，不自行重算——
   特征值与分析报告使用同一套指标口径，杜绝"两套 EMA"漂移。

## 二、特征总表（列 → 概念来源 → 输出性质）

### 层0 K线质量（v3 新增）

| 列 | 公式 | 概念来源 | 性质 |
|---|---|---|---|
| bar_quality | range < 0.15×ATR14 → thin，否则 normal | 概念适用范围：M24 全部比率锚点隐含"K线有可测波动幅度"；tick 量化噪声主导时分类失效 | 事实 |

**thin 的输出契约**：比率派生分类列（close_third/big_body/doji/trend_bar/上下影>实体2倍）输出 None；
比率原始数字（body_pct/shadow_pct/close_pos）照常输出（透明）；集合关系类（inside/outside/pattern/
swing/bo/magb/ft）照常输出（不受幅度影响）。

### 层1 几何事实（M24 §二/§三、M43）

| 列 | 公式 | 概念来源 | 性质 |
|---|---|---|---|
| dir | close vs open | M24 多头K/空头K | 事实 |
| body_pct | \|c-o\|/(h-l) | M24 实体力量对比 | 数字（thin 上为 tick 噪声，AI 降权） |
| upper/lower_shadow_pct | 影线/(h-l) | M24 | 数字（同上） |
| close_pos | (c-l)/(h-l)，U/M/L=三等分 | M24 §二收盘位置法则（上/中/下1/3） | 数字+锚点分类（thin→None） |
| big_body | body_pct≥0.70 | M24"实体占总K线70%以上" | 锚点分类（thin→None） |
| upper/lower_shadow_gt_body2x | 影线>2×实体 | M24"上影线>实体2倍" | 锚点分类（thin→None） |
| doji | body_pct≤0.10 | M24"开盘约等于收盘"——**"约"为操作化参数** | 操作化分类（thin→None） |
| inside / outside | 内包/外包（外包=双侧超出） | M24 §三、M43 | 事实（含 thin，几何关系不受幅度影响） |
| pattern | ii/iii/ioi/ioib/oio/oo | M43 §九变体全表（标注在模式最后一根） | 事实（含 thin，AI 结合 bar_quality 判断薄片期模式可靠性） |
| trend_bar | 大实体+阳收U/阴收L | M53 趋势棒词条"大实体、小影线、收盘在极端位置" | 锚点组合分类（thin→空） |
| outside_up/dn | 外包且收/开比较 | M53 OU/ODB 词条 | 事实 |

### 层2 比率数字（AI 对照知识库阈值使用）

| 列 | 公式 | 概念用途 |
|---|---|---|
| overlap_prev | 全幅交集/并集 | M02 决策树"重叠率<30%/≥60%"阈值（作者扩展口径） |
| body_overlap_prev | 实体交集/实体并集 | M45 §一突破本质"实体之间几乎没有重叠"口径——两口径并列，AI 按用途选 |
| gap_up/down_atr | 与前根外缺口/ATR | M24"缺口（强力）"、M09 缺口分析 |
| range_atr / body_atr | 全幅·实体/ATR14 | M02 决策树、K线相对大小；range_atr 兼作 thin 判定输入 |
| vol_x_prev / vol_x_avg20 | 量/前根量、量/近20根均量 | 触发条件"量≥前根1.5×"（plan 惯例）、M02"量>2×avg" |
| body_x_avg5 | 实体/前5根平均实体 | M02"实体>3×avg"引爆点判定 |
| dist_ema20_atr | (c-EMA20)/ATR14（带符号） | 均线距离、M09 MAGB 语境 |
| ema_rel | 收盘 vs EMA20（above/below/touch） | M24 §四-3 均线关系语境 |
| magb | h<EMA20 或 l>EMA20（单根不触及） | M03 MAGB（Brooks 原文定义） |
| ema_gap_streak | **同一侧**连续不触及 EMA20 计数（方向翻转重计） | M03 20GB（连续20根=计数到 20+；v3 修正为单侧口径） |

### 层3 结构事实

| 列 | 公式 | 概念用途 |
|---|---|---|
| bo_up / bo_down | h>前5根最高 / l<前5根最低 | M45 §二"前高/前低突破"——**窗口5为操作化参数** |
| ft_up/dn_trig_1/2 | 后1/2根 high 越过本根 high（含影线） | 进场触发口径（stop order 物理触发） |
| ft_up/dn_close_1 | 后1根 close 越过本根 high | Brooks 跟随确认（收盘越极值=更强确认）——**双口径并列，修正了 2PA 只看收盘的偏差** |
| swing | 局部极值（高/低于左右各2根） | M62 摆动结构——**wing=2 为操作化参数** |
| swing_type | 相对前一个同向 swing：HH/HL/LH/LL/EQ | 事实性结构分类（EQ=与前同向摆动等高，M15 §8.1 双顶/底候选素材；首个摆动无前参照不标） |
| swing_diff_atr | 相邻同向摆动点差的 ATR 倍数 | M39 微双顶/底判定素材——**只给数字，"大致相同"由 AI 判** |

## 三、操作化参数表（非知识库原文，改动需在此登记）

| 参数 | 默认值 | 依据 |
|---|---|---|
| THIN_RANGE_ATR | 0.15 | 薄片判定（v3 新增）；实测 XAU 5m 凌晨 42/100 根 range≤0.05 全捕获、BTC/ETH 15m range_atr≥0.34 零误伤 |
| DOJI_BODY_PCT | 0.10 | M24 原文"约等于"的容差选择（v2 从 0.25 收紧：0.25 致 XAU 5m 十字星率 30% 过宽；2PA 用 0.25） |
| BREAKOUT_LOOKBACK | 5 | 前高/前低回看窗口（M45 未给窗口数） |
| SWING_WING | 2 | 摆动点判定窗口（M62 未给窗口数） |
| WARMUP | 30 | vol_avg20/前摆动预热根数 |
| L1_BARS | 20 | 分层加载 L1 逐K几何表根数（LLM 逐K粒度上限，工程约定"20 几何特征"） |
| L2_BARS | 5 | 分层加载 L2 最近逐根摘要根数（工程约定"5-bar summary"） |
| VOL_2X_AVG | 2.0 | L0 量能计数阈值（M02"量>2×avg"） |
| MAGB_STREAK_MIN | 5 | L0 最长 MAGB 连续段只报 ≥5 根（短段无结构意义） |
| DBL_TOP_TOL_ATR | 0.02 | M39 双顶/底两同向摆点价差容差（×ATR；ATR 缺失回退相对差 2%）——窗口级序列原语（§十） |
| HS_SHOULDER_TOL | 0.03 | M15/M27 头肩右肩相对左肩高度容差（相对比例）——窗口级（§十） |
| WEDGE_DECREASE_MIN | 0.10 | M15/M03/M47 楔形三推幅度递减下限（d2<d1×(1−此值)）——窗口级（§十） |
| SHRINK_STAIRS_N | 3 | M41/M02/M31 收缩阶梯比较段数——窗口级（§十） |
| CLIMAX_REV_BODY_MIN | 0.70 | M33/M01/M15 高潮反转末根反向K实体占比下限——窗口级（§十） |
| RETRACE_SHALLOW | 0.382 | M36/M10/M28 回调深度浅档上界（fib 0.382）——窗口级（§十一） |
| RETRACE_MID | 0.5 | 回调深度正常档上界（0.5 分水岭）——窗口级（§十一） |
| RETRACE_DEEP | 0.618 | 回调深度深档上界（fib 0.618；>此值为过深 over）——窗口级（§十一） |
| PULLBACK_BINS | (1,L0)(2,L1)(5,L2)(10,L3)(20,L4) | M62/M28/M36 回调K计数分档（连续逆势K根数；>20 归 L5）——窗口级（§十一） |

## 四、薄片K线（v3 核心修正）

**问题**：低波动时段（如 XAU 5m 凌晨）大量K线 range 仅数个 tick，tick 量化噪声主导比率计算。
实测 XAU 5m 100 根中 42 根 range≤0.05（ATR14=0.375），v2 产出 65% 假"大实体"、
49% 假"内包"、23% 假"十字星"——1-tick range 的 body_pct=1.0/收位极端是量化噪声，不是多头力量。

**处理**（非数据修复，是概念适用范围的忠实处理）：bar_quality 列输出 thin 标记；
比率派生分类不适用输出 None；集合关系类照常。薄片期的"高密度模式/摆动"仍是几何事实，
其可靠性由 AI 结合 thin 标记判断（分工定则不变：程序只说"哪里有什么"，不说"值不值得参考"）。

## 五、AI 使用规则

1. **可直接引用为事实**：dir/inside/outside/pattern/big_body/close_third/magb/swing_type/bo_*/ft_*
   （thinK线上的比率分类列为 None，引用时跳过）
2. **必须由 AI 套阈值后使用**：overlap_prev（对照 M02 阈值）、swing_diff_atr（判定微双顶）、
   vol_x_*/body_x_avg5（对照方案触发条件）、range_atr（对照周期波幅预期）
3. **薄片期降权**（v3 新增）：bar_quality=thin 密集出现的时段（如凌晨低流动性），
   其模式/摆动/突破标注虽为几何事实，AI 应按"铁丝网/低可靠性环境"语境处理
4. **写报告时**：特征表数字可直接作为"数据复核记录"的证据源（程序算出 vs AI 抄录的交叉验证）
5. **禁止**：把特征表任何一列当作"程序意见"转述（例：禁写"程序认为该K线是强多头"——程序只给 0.85U 这个事实）
6. **形态位置意义归 AI**：程序只负责"哪里出现了 ii/iii/ioi/oio/oo/ioib"（几何识别）；
   "该位置是否有意义、值不值得参考"由 AI 按知识库判断——同一形态在趋势中/区间边界/区间中部/趋势末端
   含义不同（M43 §五、§八：趋势中=暂停可能顺势突破；区间边界=蓄势；区间中部=不交易；末端=可能最终旗形；
   铁丝网=全部不可靠）

## 六、分层阅读顺序（先窗口后逐K）

**动机**：全量特征表把窗口视野（100-300 根）与逐K粒度混在一起，注意力焦点会丢。分层阅读把输入组织成三层，**按「先窗口后逐K」的顺序消费**。

**三层结构**：
- **L0 窗口级结构摘要**（全局视野）：摆动序列（时间/价格/kind/type，看 HH/HL/LH/LL 判断
  趋势/区间）、模式分布（M43 变体计数 + 最近5处）、量能分布（≥2×avg20 计数 + 峰值 top3）、
  MAGB 连续段（≥5根，M03 20GB）、缺口计数（M24/M09）、破5计数（M45）、大实体/趋势棒计数
  （M24/M53）。**全部只是事实统计，不含任何「趋势/区间/信号质量」结论**——走势分段与市场状态
  判断由你基于这些数字完成（形态分工定则不变）。
- **L1 最近 20 根几何表**（逐K粒度）：本文 §二 的 26 列特征，只取最近 20 根。
- **L2 最近 5 根逐根摘要**（最近焦点）：K5-K1 每根一句话事实（方向/实体%/收位/重叠%/量×前/
  量×均/幅×ATR/距EMA/MAGB/模式/摆动/破5/跟随触发/薄片标记）。

**边界纪律**：L0 只出事实统计；L1/L2 数字与 L0 同一套公式口径；
薄片K线在 L2 摘要标「薄」并跳过收位分类（比率分类不适用，原始数字照常）。

## 八、验证记录

- 多品种（XAU 5m/BTC 15m/ETH 15m）× 多维度（thin/IB/OB/模式/摆动极值性/streak/MAGB/缺口/破5/大实体/趋势棒/MAGB段）独立重算对账 0 错误；L0 十维、L1 根数=20、L2 摘要根数=5 完整；thin 正确隔离（L2 摘要标「薄」、跳过收位分类）；JS 运行时冒烟通过。

---

## 九、与其他实现的口径差异对照（C 类核验留档）

> 知识库对比差异报告（2026-08-24）C 类口径差异核验。本表只**记录差异留档**，不改变本口径。对照对象：`2pa-agent-rust/src/data/geometry.rs`。

| # | 项 | 本口径 | 2pa-agent-rust geometry.rs | 分析 | 处理 |
|---|-----|-------------------|----------------------------|------|------|
| C1 | doji 阈值 | `doji = body_pct ≤ 0.10`（§27 从 0.25 收紧） | `doji = body_ratio ≤ 0.25` | 0.10 更贴近"实为十字星"，0.25 致 XAU 5m 十字星率 30% 过宽 | **保留 0.10**，差异留档 |
| C2 | 趋势棒 | `big_body`(≥0.70) + `close_third`（收位三等分）组合判定 | 收位分类：阳 `close_position≥0.65` / 阴 `≤0.35` | 判定维度不同（实体占比+收位 vs 仅收位），各自合理 | 记录差异 |
| C3 | 内包序列 | 按 K 线实际连续内包实时计数 | 固定回溯 `prev~prev3` 输出 `iii`(连续3)/`ii`(连续2)/`ioi`(内-外-内) | 计数窗口/范围不同 | 记录差异 |
| C4 | follow-through | `ft_up/dn_close_1/2` 后向看后续收盘是否越本根极值（双口径，§30） | `follow_through_1_2` 前向看后续 1-2 根 | 方向&窗口不同；本技能已修正"只看开盘不看跟随"偏差 | 记录差异，保留当前 |
| C5 | 附加判定 | — | 额外含 `breakout_prev_range`（前 N 根突破）、`bar_type` 更细（trend_bull/bear/flat…） | Rust 有、features 未覆盖的增量判定 | 记录，如需可在特征层补 |

---

## 十、窗口级序列原语（B类下沉批1 反转结构族，v5 新增）

**定位**：§二层0-3 为逐K特征表；本节为**窗口级序列判定原语**——输入整段已收盘K（最新在尾部），
输出枚举/布尔/None。供执行侧的 sequence 条件判定消费（dbl_top/dbl_bot/hs_top/hs_bot/
wedge/shrinking_stairs/climax_reversal，词表见 plan-schema_part1.md），与执行侧原语
同名原语契约对拍（test_cross_copy_contract 把关）。边界纪律同 §一：只出几何事实，
禁方向观点/质量评价（dbl_top=bear 结构等仅为结构命名，非交易建议）。

| 原语 | 签名 → 返回 | 概念来源 | 判据摘要 |
|---|---|---|---|
| double_top_bottom | (candles, atr=None, order=SWING_ORDER) → "dbl_top"/"dbl_bot"/None | M39/M03/M15/M28 | 最近两同向摆点价差 ≤ DBL_TOP_TOL_ATR×ATR（ATR 缺失回退相对差 2%）∧ 最新收盘破两峰/谷间极值（颈线） |
| head_shoulders | (candles, atr=None, order=SWING_ORDER) → "hs_top"/"hs_bot"/None | M15/M27 | 最近 3 同向摆点中峰最高/中谷最低；顶部右肩 ≤ 左肩×(1+HS_SHOULDER_TOL)、底部右肩 ≥ 左肩×(1−HS_SHOULDER_TOL) ∧ 最新收盘破两肩间反向摆点极值（顶部=两谷较高者，底部=两峰较低者） |
| wedge_triple_push | (candles, atr=None, order=SWING_ORDER) → bool | M15/M03/M47 | 最近 3 次同向推动单调推进且 d2 < d1×(1−WEDGE_DECREASE_MIN)（动能衰减，无方向） |
| shrinking_stairs | (candles, atr=None, order=SWING_ORDER, n=SHRINK_STAIRS_N) → bool | M41/M02/M31 | 最近 n=3 段同向推动高度严格递减 d3<d2<d1（无方向） |
| climax_reversal | (candles, atr=None, min_bars=3) → bool | M33/M01/M15 | 末根前 climax_leg 成立（≥min_bars 根同向大实体扩张）∧ 末根反向且实体占比 ≥ CLIMAX_REV_BODY_MIN（无方向） |

**缺数据语义**：K线不足/摆点不足/ATR 不可得 → None 或 False（保守不猜；上层 signal_eval 对
sequence 条件转 manual，fail-safe）。**params 覆盖预留未启用**：neck_break/decrease_min/
min_leg_atr/body_ratio_min 及 dbl_* 的 tolerance_atr 扩展当前不消费——程序按 §三头部常量判定，
计划写入被忽略；调整阈值须改常量并走五步契约全同步（feature-addition-contract.md）。

---

