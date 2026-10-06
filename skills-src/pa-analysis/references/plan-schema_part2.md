> 本文件是 `plan-schema_part1.md` 的第 2/6 片（按 `##` 小节切分，内容未改动）。

## order_type 语义（Gate 实现映射 · 0829 契约对齐）

节点3 只认开仓机械路径 `limit | stop | stop_limit`；`market` 仅 manage 型（紧急平仓语义，走 market_close 路径）。entry 型带 `market` 或未知值会被节点3 拒载（软失败进 tick summary），缺省时按带与现价位置推断（旧报告兼容）。

> **唯一权威字段**：订单类型以方案级 `order_type` 为准（`entry_zone` 不含 `type`，勿在此另写；节点3 已统一只读 `order_type`）。
> **术语提醒**：`order_type=stop` 是「开仓条件触发单」（突破/延续入场），**不是**「止损离场单」；真正的止损离场单在 `exit_rules.stop_loss`（条件单 + `reduce_only=true`）。

| order_type | Gate 实现 | 用途 | 执行侧布防形态 |
|---|---|---|---|
| `limit`（现价单） | `/orders` 限价单（gtc） | 回踩/回抽入场 | 带内阶梯多档（距现价升序，权重分配） |
| `stop`（开仓条件触发单，非止损离场单） | `/price_orders` 条件单（开仓触发单）：trigger{rule 1≥/2≤, price_type 最新价} + initial 限价=触发价 | 突破/突破延续入场（价格行为学最常用） | 单一触发档挂带沿，触发后限价成交 |
| `stop_limit` | 同 `stop`（Gate 条件单原生即 stop-limit，别名归一化） | 同上（显式声明触发后限价） | 同 `stop` |
| `market` | `/orders` 市价单（ioc） | **仅 manage**：紧急平仓；entry 型不支持 | — |

开仓/平仓区分：平仓订单（止盈止损）为条件单 + `reduce_only=true` 且禁市价（限价+trigger-limit-price）；开仓条件单无 reduce_only、允许触发后市价。触发规则速查：做多止损（价跌触发）rule 2、做多止盈（价涨触发）rule 1、做空止损 rule 1、做空止盈 rule 2。详见节点1 `执行侧订单分类学`（订单分类学唯一权威源）。

> **判侧口径（2026-09-10 增补）**：「回踩/回抽」与「突破」侧以带中值与现价相对位置判定（多头带中<现价=回调侧→limit；带中>现价=突破侧→stop/stop_limit，空头镜像），与节点3 `执行侧入场类型推断`、交付门禁 29 同口径。措辞约定：「回测/回踩」专指提前挂单的回调接货；信号K确认版突破回撤写「突破回撤/BOP/PBT」（突破族统一止损单，2026-09-09 裁定）。

### 订单类型 × 挂单时序（预挂架构运行时语义 · 2026-09-12）

执行侧布防按 `entry_gate_mode` 分流（`执行侧序列判定`，spec: preplace-entry-architecture D1/D4），**trigger_conditions 的运行时消费方式随订单类型不同**——节点2 字段写法不变，但报告叙事不得误述「全部触发条件确认后才挂单」：

| order_type × 触发条件构成 | 布防时序 | trigger_conditions 运行时消费 |
|---|---|---|
| `limit`（任意条件构成） | **布防即直挂** GTC 入场梯次（入场单→SL→TP 顺序全单预挂，D1 纯机械） | **仅供上下文展示，不作入场门禁**；仅 `invalidation_conditions` 的 fatal 失效线参与撤单判定（D2：信号K 不参与撤单） |
| `stop`/`stop_limit` 且 trigger 含 `signal_bar`/`sequence` | **信号K 确认后挂触发单**（触发价=信号K 另一端 1 tick，D4） | 仅 signal_bar/sequence 被消费为**入场锚**；其余类型（always_in_consensus/momentum_check/risk_reward_check/market_state/price_in_zone/volume）转观察数据（快照展示不拦截），由节点2 出计划时自核把关 |
| `stop`/`stop_limit` 且纯价格类触发 | **布防即直挂**触发单 | 价格触发即成交；trigger 仅供上下文 |

- **保护单联动（D3）**：SL/TP 与入场单同批预挂（Gate 无原生 OCO，程序侧对账联动）；成交后程序持续覆盖核验（缺单幂等补挂 → 连续失败市价平仓兜底）。
- **「共识/RR 由节点2 出计划时把关」的落点**：带中值入场实算 RR ≥ min_rr（validate 门禁 28）、逆势方案仓位 ≤50%（门禁 36）已程序化把关；节点2 报告叙事应声明「触发条件中的共识/RR 为计划质量自核项，非节点3 运行时门禁」。

## 条件 params 字段（双通道机器可读化，可选）

每个 `trigger_conditions[]` / `invalidation_conditions[]` 可带可选 `params` 对象，把 description 中的量化参数结构化，供程序参数化校验（节点3 signal_eval）与监控员 AI 判决契约（form_status）消费。**形态类条件缺 `params` → 程序判 manual，转监控员 AI 判决；带 `params` → 程序可参数化校验，AI 仍可监督纠偏。**

| type | params 字段 |
|---|---|
| `signal_bar` | `pattern`(11 种登记枚举，见下方「信号K params 词表规范」)、`direction`(bull/bear)、`body_ratio_min`、`close_position_min`(做多收上端)、`close_position_max`(做空)、`max_range_atr`、`within_entry_zone`(bool)、`or_long_wick`/`or_lower_wick`、`timeframe`(可选) |
| `sequence` | `sequence`(必填名，63 种登记枚举见「信号K params 词表规范」)、`direction`(bull/bear，纯几何序列可不带；批2 中仅 retrace_*/two_leg 读它做可选限侧)、`timeframe`(可选)、`tolerance_atr`(默认 0.02，mdb/mdt 用)、`variant`(two_bar_reversal 变体 standard/engulfing/flat)、`expect`(breakout_quality/breakout_pullback/channel_class/range_position/gap_class/gap_strength/tf_alignment 用，见词表)、`min`/`max`(channel_overshoot/tight_range_duration 用)、`expect_kind`/`max_dist_atr`(magnet_rank 用)、`htf`/`mtf`/`ltf`(tf_alignment 三层 interval 键，至少 htf 必填) |
| `momentum_check` | `min_consecutive_bars`、`direction`、`follow_through`(bool)、`min_range_atr`、`min_volume_mult`、`max_consecutive_bars`(批2，20根法则反向门：连续同向>此值→not_met)、`failed_h12`(bool，批2，失败H1/H2判定)、`r_factor`(默认 1.0，failed_h12 的 R 倍数门槛) |
| `risk_reward_check` | `min_rr`(默认 1.0)、`max_stop_atr`(止损 ≤ N×ATR) |
| `market_state` | `expect`(breakout/narrow_channel/wide_channel/range/barb_wire/neutral 英文别名，或节点3 中文枚举直通；批4 铁丝网新类型，程序比对 features.market_state 返回值，铁丝网=重叠率≥80%∧震荡K≥60%∧均幅<2×ATR 硬判)、`timeframe`(可选) |
| `always_in_consensus` | `expected`(**long/short/neutral，禁写 ais/ail 缩写**——2026-09-11 W9 裁定：actual 侧词表（watcher 归一化/程序化自算）固定 long/short/neutral，缩写失配致失效条件永久 not_met；程序侧已加归一化兜底，交付侧仍须写规范词，validate 第 37 项把关)、`timeframes`(如 ["1h","15m"])、`strict`(bool，false=允许 neutral 不否决) |
| `candle_close` | `interval`、`operator`(`<`/`>`)、`price`、`close_position_min/max`、`or_long_wick`、`or_lower_wick` |
| `volume` | `min_mult`(相对基准倍数)、`baseline`(`prev`\|`avg20`，缺省 avg20 向后兼容；「信号K量≥前根」语义须显式声明 prev，2026-09-12 变更 9/P2-6)、`interval`(可选)、`min_abs`(可选) |
| `price_in_zone` | `zone`(lower/upper，缺省回退 entry_zone)、`on_close`(bool，收盘价判定；tf 缺省取计划执行周期，无该周期 K 线 → manual，2026-09-12 变更 9/P2-5 已实现) |
| `price_above_ema` | `side`(above/below)、`timeframe`、`ema_period`(默认 20) |
| `price_breakout` | `level`、`side`(above/below)、`on_close`(bool) |
| `time_elapsed` | `max_seconds` 或 `max_atr_mult`(按 ATR×K 数) |

### 条件级 AI 参与策略 `ai_policy`（可选，节点3 AI 软增强非必需）

每个 `trigger_conditions[]` / `invalidation_conditions[]` 可带可选 `ai_policy` 字段，声明该条件在合成裁决中对监控员 AI 的依赖度。**缺省按类型推导**（显式字段优先，非法值回退类型默认）：

| ai_policy | 语义 | 类型缺省 |
|---|---|---|
| `required` | 程序判不了、AI 判决必需——AI 缺席/forming/not_met → 阻塞（保守等 AI） | 无 params 形态类 / 未知类型 |
| `preferred` | 程序几何判据为硬基线、AI 软否决——AI 缺席/forming/过期 **不阻塞**（突发行情程序自足放行）；AI 在场 not_met → 软否决拦下，invalidated → 致命 | 带 params 形态类 |
| `inform` | 纯机械、AI 仅参考——AI 不参与放行门禁 | AUTO 6 类 |

> **设计原则（2026-08-29 程序自足）**：程序几何判据（params 参数化校验）是默认基线，AI 是软增强、不是必需。突发行情窗口没时间等 AI 出结果时，带 `params` 的形态类条件由程序几何判据自足放行（快照 `ai_baseline` 留痕降级）；AI 场后出结果仍可软否决（not_met → `ai_conflict` 修正拦下）或致命（invalidated → 走失效链路），监督不因自足放行而缺失。无 `params` 的形态类/复杂形态程序判不了，保持 `required` 保守等 AI。

**判定归属**：
- **程序可判（机械）6 类**：price_in_zone / candle_close / price_above_ema / volume / price_breakout / time_elapsed——无需 params 即可判。
- **形态类 6 类**：signal_bar / sequence / always_in_consensus / momentum_check / risk_reward_check / market_state——带 `params` → 程序参数化校验；无 `params` → manual（转监控员 AI）。
- **复杂形态分两批下沉**：双底/双顶/头肩/楔形三推/收缩阶梯/高潮反转已下沉为 sequence 程序判定（批1 反转结构族）；H1/H2/L1/L2 回调计数、回调深度分档、回调K计数 L0-L5、两腿回调、20根法则反向门、失败H1/H2 亦程序判定（批2 趋势回调族）。**仍归监控员 AI 的复杂形态**：通道形态、二次入场、最终旗形、区间陷阱、尖峰态专属信号等纯语境条目（pattern-catalog #20-#24，须标 `ai_judged: true`）。

**典型合成示例（节点2 计划"价格到区 + 反转信号K"）**：`price_in_zone`（程序判价在区）∧ `signal_bar`（AI 判反转K形态）——程序 met + AI verdict=met 才执行；AI 判 forming/not_met → 继续等；AI 判 invalidated → 走失效链路。若 `signal_bar` 带 params（可几何判反转K），则该条件默认 `preferred`——AI 缺席时程序几何判据自足放行（快照 `ai_baseline` 记降级），AI 在场仍可软否决。

**plan_type 方案类型（缺省=entry，向后兼容）**：
- `entry`：新开仓方案。`entry_zone` 是真入场带（布防触发语义），止损必须在带外——节点3按链条不变式校验（见下）。
- `manage`：存量持仓处置方案（止损上移/分批止盈/离场指令，不新增风险）。`entry_zone` 语义降为处置参考带（informational，链条校验豁免）；方向校验改用现价（见 manage 规范）。节点3走采纳流（对账账簿外仓位 → 按 exit_rules 挂 SL/TP），不布防入场单。

