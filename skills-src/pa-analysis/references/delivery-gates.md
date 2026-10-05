# 交付门禁（44 项）

> 从 `validate_report.py` 的模块说明提取。**bot 不产出报告，所以这些门禁不会在交付时自动跑** —— 它的价值在于：它逐条写明了「一个完整方案必须自洽到什么程度」，你可以把它当作**方案自检清单**用（尤其 25 链式不变式、28 RR 自洽、30 失效对应、33 时间口径、34 形态词表）。

```
pa-analysis 报告物理完整性验证器 — 交付前最后一道机器门禁（对应 verification-checklist.md A6）

用法:
    python validate_report.py <报告路径.html> [--profile full|plan]
    python validate_report.py <plan.json>            # F10 计划阶段预检：裸 plan-json
                                                     # 自动包装强制 plan 档（同口径）

检查 44 项（full 档全跑；plan 档跳过 15 项 html 层=检查对象为 HTML 人读面/图面的项
（1/2/7/8/12/13/14/15/19/21/22/23/24/27/32b），用于 auto 模式壳校验，plan 层 29 项硬门禁不变），
全部 PASS 退出码 0；任一 FAIL 退出码 1（禁止交付）。
仅用标准库。1-8 物理完整性；9-15 内容级存在性门禁（plan-json 顶层 11 字段缺失 / entry_reason_type 缺 / data_source 类型错 / 快照节缺 /
交易者方程式缺 / 23 理由矩阵缺 / 映射表缺）；16 方案交接（重分析未逐方案对账旧版 / 首份未标注无前版）；17-19 执行链完整（
exit_rules 六件套缺 / position 结构缺 / HTML 离场规则人读面缺——节点3 依赖执行链全字段）；
20 量能门槛机器可读（trigger_conditions 正向下限或
no_trade_conditions 负向否决皆可——冻结时段薄量信号默认假，SKILL.md 工作流步骤 11）；
21 JSON↔HTML 关键价一致（active 方案执行链关键价必须
出现在 HTML 人读面——一份报告两个面必须同一份事实；语义级两面对齐归 SKILL.md 步骤 9 制作者数据复核）；
22 快照节形态（四列表 + 无占位文字——0824 事故：占位"将在…回填"未回填仍过门禁）；
23 无可见 JSON 展示块（0826 事故：第 11 节曾加可见 JSON 转储——计划 JSON 只允许存在于隐藏 script 块）；
24 方案块表格化（0824 形态漂移：primary 方案交易链 7 段须两列表呈现，watch/backup 方案块允许
结构化文字（0823/0826 先例）；报告骨架 4.x 已预置表格）；
25 entry 型链条不变式（多头 SL<带下沿<带上沿<T1、空头镜像，严格不等，相等即断裂——与节点3
plan_loader._validate_consistency 逐字一致；0827 事故：B方案 SL==带下沿被节点3整版拒绝）；
26 manage 型方案规范（plan_type=manage 存量处置：方向校验改用现价、position.risk_pct=0 不新增风险、
combination_rules.primary 不得指向 manage 方案——manage 不作主入场路由）。plan_type 缺省=entry（向后兼容）；
27 图面质量（标准渲染器机读属性 data-vol/ema/candle-w/ann/zones/labels：图源必须为 scripts/chart_svg.py
产物、量能条与EMA20全图强制、蜡宽≥6.5、事件标注框≥2、入场带≥1、训练层短标≥1——0829 事故：三个会话
临场手写渲染脚本三代漂移，量能条/标注框/价签盒逐代丢失仍全过旧门禁；规范见 annotation-spec.md 绘制工具链；
0829 二轮审计增补：时间锚定（最后K 须 ≤ data_time+1根——图面禁止含分析后行情，重绘必须传 end=data_time）、
价签盒防重叠（垂直间距须 ≥16px，chart_svg 自动错开）——两项皆为纯几何检查，不判内容对错）；
28-33 plan-json 自洽门禁（方案层内部一致性，非行情对错判断）：
28 RR 自洽（risk_reward_check 声明的 min_rr 必须与带中值入场的关键价实算一致——RR 死锁=假优势计划）；
29 订单类型语义（2026-09-10 重构为几何主判：带中值在现价回调侧须 limit、突破侧须
stop/stop_limit——与节点3 plan_loader.infer_entry_type 同口径；突破族 sequence
（breakout_ignition/breakout_quality/breakout_pullback/pbt/trend_line_breakout）硬约束
stop（2026-09-09 全网核实裁定：突破族统一止损单入场，覆盖 51 规则8 限价单旧表述）；
关键词降级为措辞/几何交叉校验（纯突破词配回调侧带、纯回测/回调词配突破侧带均 FAIL）；
manage 型豁免、order_type 缺省或带位/现价数据不足跳过）；
30 失效处理对应（invalidation_conditions 的 id 集合必须等于 lifecycle.invalidation_handling 的
ref 集合——失效条件漏接处置=触发后无人接管）；
31 四格矩阵（swing/scalp × long/short 全覆盖，watch 方案亦计入；缺格须顶层 plan_matrix_notes
申报豁免（style+direction+reason 三字段齐）——单侧风格=分析视角盲区）；
32 占位残留（递归遍历 plan-json 全部字符串值，"X点/待填/TBD/TODO" 清零——TODO/TBD 大小写不敏感，
"X点" 为字面两字符；__FILL 归第 1/3 项，不重复报告）；
33 时间口径（hold_time 上界不得超 time_stop 首档 at_hours；swing 禁分钟级离场叙事、
scalp 时间止损须 <2h——持有声明与离场规则必须自洽）；
34 形态条件 params 词表（信号K覆盖对齐 Task 4 节点2 契约硬化：形态类条件 signal_bar/sequence/
momentum_check/risk_reward_check 必须携带非空 params——缺 params 节点3 判 manual 转监控员 AI，
无 AI 时计划过期；signal_bar pattern 名须在 11 种登记枚举、sequence 名须在 63 种登记枚举
（未登记名 → 程序 manual，绝不猜）；语境类形态（尖峰暂停/二次入场/最终旗形/区间陷阱/SPS 等，
#20-#22/#24）须标注 ai_judged:true 归口监控员 AI——缺 ai_judged 仅记 warning 不阻断，
缺 params/未登记形态名为 violation 阻断交付。词表与 pa-executor/core/signal_eval.py 逐字一致）；
32b HTML 模糊占位（2026-09-11 W6：正文数字+xx 式模糊占位（"78,3xx"）——真实数据须精确值，
全表唯一占位式写法曾漏过旧占位扫描；扫正文（剥 script/style），与 32 的 plan-json 侧互补）；
35-37 溯源/仓位/词表自洽门禁（2026-09-11 复核意见书 BLOCKER1/W7/W9 落地）：
35 现价溯源（报告 market_context.current_price 必须与冻结库 as-of 1m 收盘对表，偏差 ≤0.1%——
锚点价疑混入锚后实时价即"可溯源至冻结库"声明不实；meta.data_source.{kline_db,data_time}
缺失/库丢失/无 as-of 棒皆 FAIL）；
36 逆势仓位（market_context.always_in 归一化背景（枚举直通/ais-ail 映射/精简关键词兜底，双方向
并存或缺失跳过）下，逆势方案 position.pct_of_standard ≤50 或含趋势线突破依据或显式豁免申报
——framework F.3.1/F.3.4；manage 型存量处置豁免；≤50 但无依据记 warning）；
37 失效条件词表（trigger/invalidation 的 always_in_consensus 条件 params.expected 必须用
actual 侧规范词表 long/short/neutral——写 ais/ail 缩写（2026-09-11 AIS bug 同型：节点3 曾
永久 not_met）或未登记值 FAIL；expected 缺失仅 warning（程序有方向反向默认））；
39 primary 唯一性（per-plan-independent-runtime T3.2 2026-09-13：active entry 方案中
priority=primary 恰好 1 个——primary 是节点2 的排名观点（最优方案）与 G7 预算截断时的
布防顺序参考，不再作执行路由（每个 active entry 方案独立布防）；双 primary = 排名自相矛盾
（24h 测试 5/5 品种 A/C 双 primary 静默出厂事故），0 个 = 有 active 方案却缺排名观点；
combination_rules.primary 与 priority=primary 方案须一致（同一排名两种口径不得分歧）；
无 active entry 方案（纯 manage/全 watch 报告）跳过不误报）；
40 品种风险预算自律（per-plan-independent-runtime T3.3/D3：Σ active entry 方案
position.risk_pct > 4%/品种 → WARN 不 FAIL——四方案独立布防下 4×2%=8% 常态超出，
G7 total_risk_limit_pct=20% 组合硬顶仍是执行侧硬兜底（按 plans[] 顺序截断），
本项是节点2 出计划时的自律提示（降单方案 risk_pct 或收窄 active 集合）；
manage 型 risk_pct=0 天然不占预算，watch 方案不布防不占预算）。
41 场景 id 契约（post_entry_scenarios[].id 必填非空、计划内唯一——节点3
close_reason="scenario:<id>" 的可追溯性依赖；09-15 六笔空名离场不可复盘）；
42 premise_invalidation 契约（active entry 型方案必须含
premise_invalidation.close_below —— 节点3 前提状态机的唯一机器锚，
EMA20 回退已删除）；
43 逆AIL结构互斥（2026-09-16 F7-1：always_in_consensus 失效条件
params.expected 归一化后 == market_context.always_in → 条件入场即成立、
永不构成「翻向」＝结构退化；节点3 命中即减半，实测 5/5 笔盈利单无一触及 TP1）；
44 止损距离契约（2026-09-16 F7-2：active entry 方案 |stop_loss − entry_zone 中值|
≥ stops.min_atr×ATR（执行周期 SWING→1h / SCALP→5m）——节点3 侧「过近」
已由 DEGRADED 照常布防改为硬拒布防，本项在交付前拦截以免「方案静默不可交易」）。
只查存在性与类型，不判内容对错——内容正确性仍属 AI 的 Level A1-A5 门禁，"分析零脚本"原则不变。
```
