> 本文件是 `plan-schema.md` 的第 6/6 片（按 `##` 小节切分，内容未改动）。

## 校验规则（AI 交付时自检）
- active 的方案必须：交易链 7 段齐全、entry_zone/stop_loss/targets 非空、trigger 有 K 线证据、RR 与胜率假设不矛盾、来源在 plan_traceability 有标注。
- **链条不变式（entry 型 active 方案，与节点3 plan_loader._validate_consistency 逐字一致）**：多头必须 `stop_loss < entry_zone.lower < entry_zone.upper < take_profit.target1.price`；空头必须 `stop_loss > entry_zone.upper > entry_zone.lower > target1.price`（严格不等，相等即断裂）。违反 → 该方案 active=false（降级观察）并注明断点。
- **manage 型 active 方案规范**：`entry_zone` 为处置参考带（informational，链条校验豁免）；方向校验改用现价——多头必须 `stop_loss < market_context.current_price < take_profit.target1.price`，空头镜像；exit_rules 六件套同 entry 要求；`position.risk_pct` 存量处置=0（不新增风险）；`combination_rules.primary` 必须指向 entry 型方案（manage 方案不作主入场路由）。
- **active 的方案必须含完整 `exit_rules` 六件套**：stop_loss（price+type）/ take_profit（**数组** `[{price, close_pct}]`，有序即 TP1/TP2…）/ trailing_stop（enabled+method+stages，M13 七层）/ time_stop（enabled+rules，swing 24h 重估+72h 全平、scalp 45min 减半+90min 全平；规则单位 at_hours——节点3 双形态兼容 at_seconds/at_hours，节点2 统一落 at_hours）/ drawdown_stop（浮盈峰值回撤 50% 全平）/ scratch_exit（premise_invalid 前提否定打平）——节点3 离场执行全依赖此字段，缺任一 → active=false；方案级 stop_loss/take_profit 是"分析位"，exit_rules 里的同名子字段是"执行位"（含分批/类型），两者都必须有。
  - ⚠️ **执行位 `exit_rules.take_profit` 必须是数组 `[{price, close_pct}]`，禁止写成 dict（`{mode, target1, target2}`）。** 方案级 `plans[].take_profit` 才是 dict（含 mode/rr/description，供分析与 RR 校验）；执行位是它的**降维执行投影**，只保留可执行的价与比例。两者**层级不同、形态不同，不可混淆**——节点3 `position_adopt._manage_targets` / `position_manager._manage_exit_plan` 只认数组形态（`isinstance(er, list)`），写 dict 会被静默回退到 `tps`。（2026-09-20 P0 事故：此处契约曾误写 dict 而实际数据为 list，validate_report 第 21 项按 dict 解析在 list 上抛 AttributeError，因缺异常保护致第 21~44 项共 24 个门禁全部未执行、门禁静默失效。）
  - **单目标 vs 多目标**：单元素数组 `[{price, close_pct:100}]` = 原 `mode: single`；多元素 = 原 `mode: staged`，`close_pct` 之和 ≤100（其余走移动止损）。空数组非法（validate_report 第 17 项判 FAIL）。
- **active 的 entry 型方案必须含 `premise_invalidation.close_below`**（机器失效位）——取本方案 fatal 失效条件首条的结构位，或信号K极值/突破点/区间边界；缺 → 交付门禁 FAIL（validate_report.py 第 42 项）。`confirm_bars` 缺省按周期取（swing 1h×3、scalp 5m×3）；`interval` 与方案 `timeframe` 一致（须与节点3 `_premise_interval` 口径相同：SWING→1h、SCALP→5m）。
  - **方向语义（最易误用，必读）**：`close_below` 对**多头** = 收盘**跌破**该位；对**空头** = 收盘**站上**该位。字段名是历史遗留，**不是方向限定**。
  - **为什么必须写**：这是节点3 前提状态机（`position_manager._tick_premise`）的**唯一机器锚**。缺该字段时程序过去会回退到 EMA20——而 EMA20 被本体系明令排除在判断依据之外（`pa-analysis/SKILL.md:25` 逐字：「EMA20/ATR14 …… 均不构成入场信号」），且破位判据无强度要求，导致区间市连续 3 根小实体K 即判前提失效全平（实测 5/5 笔在 12 分钟内被全平，其中一笔距 TP1 仅 0.24%）。自 2026-09-16 起回退路径已删除：**无该字段 = 只能靠强度启发式识别质疑K**（容忍度显著降低，但不再是「收在均线下方 1 tick 即算否定」）。
  - 与 `exit_rules.scratch_exit.rules[].rule="premise_invalid"`（纯文本描述）**并存不替代**：前者是机器判据，后者是语义说明。
- **active 的方案必须含完整 `position` 结构**：pct_of_standard（AIL 共识降级时 <100）/ risk_pct / batch（swing [50,30,20]、scalp [100]）——UNVERIFIED 账户下仍须给出（基于假设资金的条件性输出），节点3 arm 前按 account_gate 复核。
- active 的方案必须含 `alternative_hypothesis`（主假设失效后的去向）与 `evidence_chain`（四段式证据链）；缺任一 → 降级观察。
- **目标分级与风格/市场状态一致（M65 §3.2 + PP-01/04）**：剥头皮（区间/宽区间/宽通道刮头皮）→ take_profit.mode=single，target1.close_pct=100、无 target2（M65 宽区间 100/0/0，PP-04 全部获利）；波段（趋势/通道/反转后）→ mode=staged，target1+target2 按 M65 §3.2 取 50/30/20、25/25/50、33/33/34 或 50/50（剩余走移动止损）；不一致 → 修正，无法修正 → 降级观察。
- 同一风格的所有方案目标结构必须一致（禁止 scalp 一个双目标一个单目标）；target1.close_pct 与 target2.close_pct 之和 ≤100。
- 同一时刻只允许一套主方案负责入场；同向止损重叠须说明加仓理由；已持有 A 时 B 出现必须能分类（持仓管理/加仓/反手/噪音）。
- active 的方案必须含 `lifecycle`，且五阶段完整：pre_trigger（未触发处理）→ trigger_confirmation → post_entry_scenarios（≥8 情形）→ invalidation_handling（与触发一一对应）→ after_stopout（禁追回+二次机会+复盘）。
- 每个触发条件（trigger_conditions）必须有对应失效条件（invalidation_conditions）；失效动作分级（fatal/warning）。
- **止损位不得与 fatal 线倒挂**：硬止损（分析位 stop_loss 与执行位 exit_rules.stop_loss.price 同值）必须紧贴 fatal 失效线的防影线一侧（缓冲仅供影线扫损），不得深越 fatal 线——深越=逻辑死区（fatal 收盘先触发撤单，硬止损在多数路径上永远不可达，等于名义有止损实际无止损）。交付前 A2 关系断言必须含"止损-fatal 位置关系"一条。
- **低流动性时段量能门控**：冻结时段（ATR 压缩至日峰极端低位+成交近零）产生的突破/破位/反转信号一律无效，默认按假突破处理；active 方案的量能门槛必须机器可读——trigger_conditions 正向下限（type=volume 或描述含"量≥X"）或 no_trade_conditions 负向否决（"量<X=夜间假信号"）**两种形态皆可**（validate 第 20 项把关）；冻结期不设 scalp 方案。
- 不满足任一 → 该方案 active=false（降级观察），并注明原因。
- account_gate.status=BLOCKED 时，所有 active 方案必须为 false。
- **交付门禁 1（RR 自洽）**：含 risk_reward_check(params.min_rr) 的方案，实算 RR=(tp1-带中值)/(带中值-stop_loss)（空头镜像）不得小于 min_rr，否则 FAIL（防节点3实算死锁）。
- **交付门禁 2（订单类型语义，2026-09-10 几何主判重构）**：带中值在现价回调侧（多头带中<现价，空头镜像）→ order_type 必须 limit（回调接货）；带中在突破侧 → 必须 stop/stop_limit（突破追认）——与节点3 plan_loader.infer_entry_type 同口径；触发条件含突破族 sequence（breakout_ignition/breakout_quality/breakout_pullback/pbt/trend_line_breakout）→ 必须 stop/stop_limit（2026-09-09 全网核实裁定：突破族统一止损单入场，覆盖 51 规则8 限价单旧表述）。措辞约定：「回测/回踩」专指提前挂单的回调接货入场，信号K确认版突破回撤写「突破回撤/BOP/PBT」——纯突破词配回调侧带、纯回测/回调词配突破侧带 = 措辞/几何冲突 FAIL。manage 型豁免；order_type 缺省或带位/现价数据不足跳过。
- **交付门禁 3（失效处理对应）**：invalidation_conditions 的 id 集合必须与 lifecycle.invalidation_handling 的 ref 集合相等。
- **交付门禁 4（四格矩阵）**：plans 须覆盖 swing/scalp × long/short 四格（active 与 watch 均计入），缺失格须 plan_matrix_notes 申报豁免（style/direction/reason 三字段缺一不可）。
- **交付门禁 5（占位残留）**：plan-json 全部字符串不得含 "X点/待填/TBD/TODO" 占位残留。
- **交付门禁 6（时间口径）**：hold_time 上界 ≤ time_stop 首档 at_hours；swing 的 post_entry 不得出现"分钟未达"表述；scalp 的 time_stop 档位 <2 小时。
- **以上 6 项已由 validate_report.py 程序化把关，AI 不得绕过。**
- **边界标注**：形态类条件 params 词表规范已硬化为上方「信号K params 词表规范」节，validate_report.py 第 34 项程序化把关（缺 params / 未登记形态名 → violation 阻断；语境类形态缺 ai_judged:true → warning）；本节其余门禁只管数字/结构自洽。

## 节点2/节点3 职责划分

| 职责 | 节点2 (pa-analysis) | 节点3 (pa-executor) |
|---|---|---|
| 计划详细度/完整性 | 全责：方案一次齐全，缺项即降级/交付门禁拦截 | 不补计划、不猜测语义 |
| 计划数字自洽 | 交付门禁（validate_report.py 程序化把关，含上方 6 项） | 加载期 RR fail-fast 复核 |
| 触发判定 | 输出结构化 params（机器可读触发参数） | 程序校验 + 监控员 AI 软否决 |
| 订单类型语义 | 断言 order_type 与入场语义匹配 | 白名单归一化执行 |
| 执行/风控/离场 | 输出 exit_rules 六件套 | 机械执行 |

**节点3不做任何分析、不补计划、不猜测语义。**

## 账户闸门与组合边界
- **account_gate**（必填，四态）：status=VERIFIED → 账户数据实时验证；UNVERIFIED → account.db 空/未接入，仓位只能条件性输出（公式 + 明确资金假设），禁止以假设资金冒充真实仓位；STALE → 有数据但陈旧（推送中断、快照超龄），仓位仍按条件性输出，note 注明快照时间与待修复事项；BLOCKED → 风控锁定/今日亏损超限，全部方案 active=false。与 meta.data_source.account_status 联动。
- **combination_rules**（多 active 并存时必填）：同一时刻只允许一个 primary；任何第二个 active 方案必须在 overlap 中写明与主方案的关系分类（持仓管理/加仓/反手/切换/噪音）与理由；主备切换走 switch_paths 明确条件；互斥方案写 mutex。这是"方案组合边界"的机器可读形式，供节点2自身与人类阅读。节点3不解析 relation 词表、不做组合判断——它按 **plan_type + 方向** 机械识别路由（primary=entry 方案布防、manage 方案走采纳流），组合的合并语义由字段结构本身承载（同向 manage+entry 组合经加仓布防机械合并）。

## 方案交接字段
- **plan_handover**（重分析报告必填；首份报告可省略或 first_report=true）：prior_report 指向所对照的旧版；reconciliation 必须覆盖旧版 plans 数组每一个 plan_id（含 watch），outcome 五选一——triggered（已触发，转复盘）/ invalidated（已失效，注明失效价）/ expired（超龄：scalp>30 分钟、swing>4 小时，参照节点3 G11）/ continued（继续等待，原样并入新报告）/ superseded（结构已变被新方案替代，note 必须归因：行情演化（数据驱动）还是判断修正（分析驱动））。**禁止静默丢弃旧方案**——这是跨会话连续性的机器可读锚点，新会话重分析时按此对账而非依赖记忆。

## fast-plan 战术计划 Schema（节点3 AI 产出 · HF-Tactical §6.2）

> **定位**：节点3 战术层（10min 高频）S2 场景专用短生命周期计划，与节点2 深析计划（本 schema 主体）**双轨并存**（spec: node23-hf-tactical-upgrade §2/D1）：同品种反向于 A-D 方案合法（per-plan 独立运行时 + dual_mode 平移），风险由品种 Σrisk 4% 与 G7 组合硬顶约束。fast-plan 走**同一条布防管线、同一 registry、同一 G1-G11 风控**（P5），布防形态 = preplace D1/D3 三单同挂（入场+止损+止盈缺一不可）。
> **写入位置**：`{plans_dir}/fast/{symbol}_{ts}.json`（独立 fast/ 子目录，不与深析计划混放）。
> **T 格位命名**：`{SYMBOL}_{date}_T{seq}_{style}_{direction}_{price}`（例 `BTC_USDT_20260915_T1_swing_long_79200`）；T 命名空间独立，不与 A-D 冲突。

```json
{
  "plan_id": "BTC_USDT_20260915_T1_swing_long_79200",
  "source": "node3-fast",
  "market_context": {
    "classification": "major_reversal",
    "always_in": "neutral",
    "spectrum_prev": "broad_channel",
    "confidence": "medium",
    "playbook_ref": "PB5-MTR",
    "order_type_rule": "stop",
    "evidence": "趋势线强势突破+测试极端+强反转K+二次确认 4 理由（M47/M48），1h 摆动序列断裂"
  },
  "direction": "long",
  "style": "swing",
  "order_type": "stop",
  "entry_zone": [79100, 79250],
  "stop_loss": 78650,
  "targets": [{"tp1": 80100, "close_pct": 50}, {"tp2": 80800, "close_pct": 50}],
  "min_rr": 2.5,
  "risk_pct": 1.0,
  "trigger_conditions": [ {"id": "t_c1", "type": "signal_bar", "description": "…", "params": {"pattern": "reversal_bar", "direction": "bull"}} ],
  "invalidation_conditions": [ {"id": "t_inv1", "type": "price_breakout", "severity": "fatal", "description": "…", "params": {}} ],
  "trader_equation": { "probability": "medium", "rationale": "双底颈线回测 + 信号K强（收上端实体85%）+ 量能1.8×（M03/M48）" },
  "knowledge_refs": ["M47", "M48", "M03"],
  "expires_at": 1789485600
}
```

**字段说明（与节点2 字段的映射/差异）**：
- `source: "node3-fast"` 必填——plan_loader 据此识别战术计划走 fast gate（深析计划走既有 G1-G11 加载校验）。
- `market_context.classification`：七态 JSON 英文枚举（`breakout`/`tight_channel`/`broad_channel`/`minor_reversal`/`major_reversal`/`broad_range`/`barb_wire`/`neutral`，词表见 pattern-catalog §一·2；中文枚举直通亦可）。`playbook_ref` ∈ PB1-BO…PB7-BW；`order_type_rule` = 该状态矩阵允许的订单类型（AI 自报，F2 交叉校验）；`confidence` ∈ strong/medium/weak（七态判定置信度，weak → 不产 fast-plan，见 §3.5）。
- `entry_zone`：**二元数组 [lower, upper]**（战术计划简化形态）；plan_loader 加载时映射为深析计划 `{lower, upper}` 对象。
- `targets`：**数组形态**（`tp1`/`tp2` + `close_pct`）；映射为 `take_profit` staged/single 模式（单目标 → single、close_pct=100）。
- `min_rr`/`risk_pct`：平铺字段；映射为 `risk_reward_check(params.min_rr)` 与 `position.risk_pct`（fast-plan 单笔 risk_pct ≤ 1.0% 硬上限，战术层半份）。
- `trigger_conditions`/`invalidation_conditions`：**词表与本 schema「信号K params 词表规范」逐字一致**（F6 校验）——程序能判的参数化全自动，纯语境条件须 `ai_judged: true`。
- `trader_equation.probability` ∈ strong/medium/weak：AI 侧胜率评估（F7 校验 weak 拒）；MTR 类低胜率 setup 要求 min_rr ≥ 2.5（§3.5 胜率补偿）。
- `knowledge_refs`：reason/rationale 中引用的 Mxx 集合，与 form_status 同纪律——每个结论必须有知识引用 + K 线证据。
- `expires_at`：unix 秒。默认 now+8h（D4 默认采行），硬上限 now+24h（F9）；到期未成交 → 程序机械撤单（不依赖 AI 记得撤）。
- **exit_rules 六件套由程序按战术缺省填充**（AI 不写，10min 节奏保持精简）：SL/TP 直挂自 `stop_loss`/`targets`（preplace D1/D3）；trailing_stop = M13 七层缺省；time_stop 与 `expires_at` 对齐档位；drawdown_stop = 浮盈回撤 50% 缺省；scratch_exit = premise_invalid 缺省。

### fast gate 十项（plan_loader 加载期 fail-fast，任一 FAIL → 拒绝布防 + 记 dispute → 转节点2 深析）

| # | 校验项 | 内容 | 口径依据 |
|---|---|---|---|
| F1 | RR 自洽 | 带中值实算 RR = (tp1−带中值)/(带中值−stop_loss)（空头镜像）≥ min_rr | 节点2 门禁 28 同口径；**classification=major_reversal 时 min_rr ≥ 2.5**（胜率补偿） |
| F2 | 状态 × 订单类型矩阵 | classification × order_type 必须落在七态矩阵允许格（见 pattern-catalog §一·2 速查表），再叠加订单类型语义交叉校验（多头带中<现价→limit、>现价→stop，空头镜像） | 矩阵为第一权威；门禁 29 语义仅交叉校验 |
| F3 | 失效处理对应 | invalidation_conditions 的 id 集合与 invalidation_handling ref 集合一致（fast-plan 缺 invalidation_handling 时程序按战术缺省生成后校验非空） | 门禁 30 同口径 |
| F4 | 占位残留扫描 | 全部字符串不得含 "X点/待填/TBD/TODO" | 门禁 31 同口径 |
| F5 | 时间口径 | hold_time 与 time_stop 档位一致；scalp 档位 <2 小时；expires_at 与 time_stop 不矛盾 | 门禁 32 同口径 |
| F6 | 形态条件 params 词表 | trigger_conditions 形态类 params 的 pattern/sequence 名取自词表（signal_eval 枚举），缺 params 形态类须 `ai_judged: true` | 门禁 33 同口径 |
| F7 | 交易者方程式 | 机械侧：RR 实算 ≥ min_rr，且 scalp 级止损单目标距离 ≥ 1.5×止损距离（6tick 赚 4tick 数学）；AI 侧：trader_equation.probability 存在且 ∈ strong/medium（weak → FAIL） | §3.4 止损单数学 + §3.5 双门 |
| F8 | 组合风险 | 该品种 Σrisk_pct（含 fast-plan + 深析计划）≤ 4%；组合级 G7 10% 硬顶 | §7 风控衔接 |
| F9 | 生命周期 | expires_at 必填且 now → expires_at ≤ 24h（默认 8h） | §6.2/D4 |
| F10 | 状态终态否决 | classification = barb_wire/窄区间 → 直接 FAIL（铁丝网不交易一票否决） | §3.2 判定规则 |

### 过度交易防线（高频层特有，spec §7）
- S2 触发前提缺一不可：七态置信度 ≥ medium ∧ 状态非禁用（F10）∧ Playbook 优先信号成立 ∧ 订单类型符合矩阵（F2）∧ 交易者方程式过双门（F7）∧ 风险预算有余（F8）。
- fast-plan 单笔 risk_pct ≤ 1.0% + 生命周期默认 8h + **同品种同时最多 2 个活跃 fast-plan**。
- **AI 侧频率自限（程序强制）**：连续 3 个 fast-plan 亏损 → 战术层自动冷静 2h（非 AI 自律）。

### 与深析计划的协同
- 节点2 数小时一次深析定框架（A-D 方案 + 结构位）；节点3 每 10 分钟快速感知，在框架内做战术调整——两层协同不冲突（D1）。
- 战术层发现结构性变化（AIL 翻转、区间破裂）→ `dispute`/`reanalyze` 召唤深析层（现有通道，零新建）。
- fast gate FAIL → 记 dispute → 转节点2 深析，本周期该品种仅推送（不重试）。
