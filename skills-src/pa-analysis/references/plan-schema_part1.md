> 本文件是 `plan-schema.md` 的第 1/6 片（按 `##` 小节切分，内容未改动）。

# 内嵌交易计划 JSON Schema（节点3 契约）

报告内嵌 `<script type="application/json" id="plan-json">{...}</script>`。节点3 可据此适配执行。
**本 schema 以 `assets/templates/plan-skeleton.json` 为结构基准（实例化源头）；本文示例与骨架冲突时以骨架为准。**
> **auto 模式壳契约（2026-09-10）**：auto 模式产物 `assets/templates/plan-shell.html` 极简壳的 plan-json 字段与本 schema **零变化**，script 块属性顺序与 report-skeleton.html 逐字一致，节点3 plan_loader 提取正则不感知模式差异。
> **每方案独立运行契约（2026-09-13，per-plan-independent-runtime）**：swing/scalp × long/short 四方案 = **四个独立计划**——节点3 对 plans 数组中每个 **active entry 方案独立布防执行**（(symbol, plan_id) 复合键多运行时并存，无互斥关系：任何方案成交不触发其他方案撤单/失效）；`priority.primary` 降级为节点2 的**排名观点**（唯一性约束保留，validate 第 39 项把关），仅作 G7 预算截断时的布防顺序参考，**不再作执行路由**；`combination_rules` 的 overlap/switch_paths/mutex = **结构演化分析备忘**（节点3 永不消费，无执行语义，见下方说明）。
> **fast-plan 战术计划契约（2026-09-15，node23-hf-tactical-upgrade）**：本 schema 主体描述节点2 深析计划；节点3 AI 产出的**战术计划（fast-plan）**走「子集 + 战术字段」的简化 schema，写入 `{plans_dir}/fast/{symbol}_{ts}.json`，经 plan_loader **fast gate 十项**机械校验后走同一条布防管线——完整契约见文末「fast-plan 战术计划 Schema」节。

```json
{
  "meta": { "symbol": "BTC_USDT", "generated_at": "ISO时间", "data_source": {"kline_db": "路径", "data_time": "最新K线时间ISO8601", "account_status": "UNVERIFIED|VERIFIED|STALE"}, "dialect": "数据表述口径" },
  "market_context": {
    "current_price": 0.0, "snapshot_time": "ISO时间",
    "always_in": "long或short或neutral（单字符串总判定；分周期明细归报告 L2.3 人读表）",
    "market_cycle": "七状态判定结果",
    "timeframe_states": { "1d": "…", "4h": "…", "1h": "…", "15m": "…", "5m": "…" },
    "liquidity_note": "时段流动性判定（如晚间冻结/正常）"
  },
  "indicators": { "ema20": { "1d": 0.0, "4h": 0.0, "1h": 0.0, "15m": 0.0, "5m": 0.0 }, "volume": "关键量能观察（高潮/冻结）", "custom": "其他指标" },
  "key_levels": { "magnets": [{"price": 0.0, "type": "支撑/阻力/磁体", "source": "前低/Mxx/图上对象"}], "day_levels": {"high": 0.0, "low": 0.0, "open": 0.0, "close_inprogress": 0.0} },
  "scenario_matrix": [ {"scenario_id": "S1", "description": "情景描述", "probability": "高/中/低", "plan_ids": ["A_…"]} ],
  "plans": [ { "单个方案结构见下节；通常 4 个（swing/scalp × long/short）。plans 数组是规范形态，validate_report.py 与节点3 均读 plans。每个 active entry 方案将被节点3 独立布防执行（四方案=四个独立计划，无互斥关系）" }, "…" ],
  "plan_matrix_notes": [ { "style": "scalp", "direction": "long", "reason": "豁免理由（可选顶层字段：plans 四格缺格豁免申报，语义见下方说明）" } ],
  "account_gate": { "status": "UNVERIFIED|VERIFIED|STALE|BLOCKED", "verified_at": "ISO时间或null", "balance": 0.0或null, "note": "状态说明；STALE 时注明快照时间与待修复事项" },
  "combination_rules": { "primary": "唯一 primary 方案 plan_id（= 节点2 排名观点，须与 priority=primary 方案一致，validate 第 39 项把关）", "overlap": [ {"plan_ids": ["A_…", "C_…"], "relation": "持仓管理/加仓/反手/切换/噪音", "reason": "理由（分析备忘：结构演化参考，节点3 永不消费）"} ], "switch_paths": [ {"from": "A_…", "to": "B_…", "condition": "切换条件（分析备忘，无执行语义）"} ], "mutex": [ {"plan_ids": ["A_…", "D_…"], "reason": "互斥理由（分析备忘——四方案独立运行，无互斥执行语义）"} ] },
  "risk_control": {
    "max_positions": "同时持仓上限（对齐节点3 G10）",
    "age_gate": "scalp>30分钟或swing>4小时的分析时戳拒绝arm（对齐G11）",
    "single_risk_cap": "单笔风险上限",
    "daily_loss_limit": "当日亏损熔断线"
  },
  "plan_traceability": { "方向": "来源", "入场": "来源", "止损": "来源", "目标": "来源", "仓位": "来源", "触发": "来源" },
  "plan_handover": { "prior_report": "outputs/旧报告路径|null", "first_report": false, "reconciliation": [ {"prior_plan_id": "A_…", "outcome": "triggered|invalidated|expired|continued|superseded", "note": "一句话依据/归因", "successor_plan_id": "新方案ID（仅 superseded）"} ] }
}
```

> **plan_matrix_notes（可选顶层字段）**：plans 数组须覆盖 swing/scalp × long/short 四格（active 与 watch 均计入）；无法成立的格必须在此申报豁免，`style`/`direction`/`reason` 三字段缺一不可；未申报缺失格 → 交付门禁 FAIL。

## 单个方案（plan）结构
```json
{
  "active": true,
  "plan_type": "entry|manage",
  "direction": "long|short",
  "style": "swing|scalp",
  "order_type": "limit|stop|stop_limit|market",
  "priority": "primary|backup|watch（primary=节点2 排名观点，active entry 方案中恰 1 个；全部 active entry 方案均独立布防，primary 不作执行路由）",
  "entry_zone": { "lower": 0.0, "upper": 0.0 },
  "entry_trigger": "信号K/入场K/跟进K/突破回测 证据描述（禁止价位口号）",
  "evidence_chain": "证据链四段式：因为A（背景）→提出B（假设）→只有C（触发）出现才执行D→若E发生假设失效、计划删除或降级",
  "alternative_hypothesis": "替代假设：主假设失效后转什么场景（如突破后回测/跌破后反手/转观察）",
  "quality_tag": "Type A=路径干净且马上触发 | Type B=撞到不利磁体 | Type C=入场前先横盘（供复盘比较）",
  "stop_loss": 0.0,
  "stop_loss_desc": "假设失效位描述（信号K极值/摆动点/趋势线外 1tick 或 1ATR）",
  "take_profit": {
    "mode": "staged|single",
    "target1": { "price": 0.0, "rr": "+1.0R", "close_pct": 100, "description": "磁体来源（前高/区间中轴/MM/整数关口…）" },
    "target2": { "price": 0.0, "rr": "+2.0R", "close_pct": 30, "description": "…（仅 staged 模式；close_pct 合计<100 时剩余走移动止损）" }
  },
  "position": { "pct_of_standard": 100, "risk_pct": 1, "batch": [50, 30, 20] },
  "trigger_conditions": [ {"id": "sl_c1", "type": "price_in_zone|signal_bar|always_in_consensus|risk_reward_check|candle_close|price_above_ema|momentum_check|volume|sequence", "description": "…", "required": true, "params": {}（可选，机器可读结构化参数，见下节）} ],
  "invalidation_conditions": [ {"id": "sl_inv1", "type": "price_breakout|always_in_consensus|signal_bar|time_elapsed", "severity": "fatal|warning", "description": "…", "detail": "…", "params": {}（可选，见下节）} ],
  "exit_rules": {
    "stop_loss": {"price": 0.0, "type": "hard_stop"},
    "take_profit": [{"price": 0.0, "close_pct": 40}, {"price": 0.0, "close_pct": 60}],
    "trailing_stop": {"enabled": true, "method": "M13七层", "stages": ["stage0_initial", "stage1_breakeven", "stage2_lock1r", "stage3_lock2r", "stage4_ema20", "stage5_target1"]},
    "time_stop": {"enabled": true, "rules": [{"at_hours": 24, "action": "重估", "condition": "入场后24小时未达预期"}, {"at_hours": 72, "action": "全平", "condition": "入场后72小时未达预期"}]},
    "drawdown_stop": {"enabled": true, "rule": "浮盈回撤50%平仓"},
    "scratch_exit": {"enabled": true, "rules": [{"rule": "premise_invalid", "description": "前提失效打平离场"}]}
  },
  "premise_invalidation": { "interval": "1h", "close_below": 76343, "confirm_bars": 3 },
  "hold_time": "4-8小时|15-45分钟",
  "timeframe": "1H/4H|5M/15M",
  "no_trade_conditions": ["区间中部", "空间不足", "信号K弱", "铁丝网", "重大事件", "无法定义止损"],
  "entry_reason_type": "对应 23 种入场理由之一",
  "lifecycle": {
    "pre_trigger": {
      "watch_interval_minutes": 5,
      "approach_distance_atr": 0.3,
      "expire_rule": "价格离开入场区超过2×ATR未触发，或挂单窗口超时（swing 4-8小时/scalp 45分钟）",
      "expire_action": "撤单 + active=false + 记录未触发观察",
      "state_override": "窄区间/铁丝网→跳过全部计划；信号组合none或conflict→triggered回退waiting",
      "near_miss_rule": "接近但未满足（如实体差2点）→ 不触发，标注接近条件，不视为已触发"
    },
    "trigger_confirmation": {
      "steps": ["信号K收盘确认", "入场K收盘确认（≥信号K极值）", "跟进K不破信号K低点/高点"],
      "trigger_invalidation_mutex": true,
      "partial_fill_action": "已成交部分执行，未成交批次作废，不补挂",
      "immediate_stop": "触发后立即挂硬止损（SL-01：信号K极值外1tick或假设失效位）"
    },
    "post_entry_scenarios": [
      { "id": "pe1", "level": "log", "condition": "浮亏0-1R且前提未受质疑", "action": "持有至止损，不手动干预", "rule_ref": "M66/BAN-03" },
      { "id": "pe2", "level": "semi", "condition": "前提被质疑（1-2根质疑K：趋势线破/回撤过深/信号K被破）", "action": "减仓50%（reduce_position）", "rule_ref": "premise_state_machine QUESTIONED/PM-05" },
      { "id": "pe3", "level": "semi", "condition": "前提被否定（连续3根质疑K，结构失效）", "action": "立即离场，不等止损被扫", "rule_ref": "premise_state_machine INVALIDATED/EX-05" },
      { "id": "pe4", "level": "log", "condition": "快速盈利+1R", "action": "平50%（scalp全平），剩余止损移至保本", "rule_ref": "PP-01/PP-04/SA-02" },
      { "id": "pe5", "level": "log", "condition": "盈利+2R", "action": "止损移至+1R；测量目标附近减仓", "rule_ref": "SA-03/EX-02/EX-09" },
      { "id": "pe6", "level": "semi", "condition": "触及目标磁体", "action": "获利了结优先，剩余收紧止损", "rule_ref": "EX-03/M37" },
      { "id": "pe7", "level": "semi", "condition": "出现强反向信号K", "action": "平仓或收紧至保本", "rule_ref": "EX-04" },
      { "id": "pe8", "level": "semi", "condition": "突破回撤失败（回撤超过关键位）", "action": "减仓或离场，转反向观察", "rule_ref": "FM-06/FM-09" },
      { "id": "pe9", "level": "log", "condition": "时间止损：scalp 15-45分钟/swing 45-90分钟未达目标", "action": "减仓50%后全平", "rule_ref": "SL-06/EX-07/M66" },
      { "id": "pe10", "level": "log", "condition": "浮盈回撤50%", "action": "立即平仓", "rule_ref": "M13第7层" },
      { "id": "pe11", "level": "log", "condition": "加仓：高概率回撤+距首批≥1R+总风险不变", "action": "允许加仓（最多2次）；否则不加", "rule_ref": "PM-04/M65" },
      { "id": "pe12", "level": "semi", "condition": "低概率形态/信号质量降级/不确定", "action": "减半或跳过", "rule_ref": "PM-05" },
      { "id": "pe13", "level": "semi", "condition": "重大事件前后5分钟", "action": "不开新仓，持仓减半或平仓", "rule_ref": "RM-06/M27#5" },
      { "id": "pe14", "level": "log", "condition": "节点1数据源中断/数据过期", "action": "挂单保留、停止新单，持仓动作转人工核对", "rule_ref": "节点1健康检查" },
      { "id": "pe15", "level": "log", "condition": "市价滑点>2tick/部分成交异常", "action": "记录并停止追单", "rule_ref": "RM-04" },
      { "id": "pe16", "level": "semi", "condition": "AIL共识破坏（swing<3/5；scalp周期翻转）", "action": "swing减仓50%；scalp全平", "rule_ref": "M20/invalidation" },
      { "id": "pe17", "level": "semi", "condition": "持仓中另一方案（B）出现", "action": "先分类：持仓管理/加仓（PM-04且≥1R）/反手（原假设失效确认）/噪音；说不清不执行B", "rule_ref": "方案组合边界" }
    ],
    "invalidation_handling": [
      { "id": "ih1", "ref": "对应触发条件的失效条件ID", "severity": "fatal|warning", "action": "fatal→撤单/全平；warning→减仓50%或收紧", "post_action": "转观察或反向激活；记录原因" }
    ],
    "after_stopout": {
      "reentry_allowed": false,
      "reentry_condition": "仅新结构（新H2/新信号K/新双顶底），同一位置禁止重入",
      "cooldown": "连亏3→冷却30分钟；日亏超每日限额（config daily_loss_limit_pct）→当日停止（RM-01/03）",
      "review_node": "节点4复盘：离场原因/MAE-MFE/九类失败/证据链",
      "calibration_node": "节点5：可复用规则需完整回放链"
    }
  }
}
```

- `exit_rules.time_stop`：档位由市况定档（铁丝网/震荡收紧 at_hours、强趋势放宽）；`condition` 必须为条件式表述（如「入场后N小时未达预期」），不得使用「无论盈亏」。
- **`post_entry_scenarios` 条目四要素（2026-09-15 第二轮修复 F3-4 / #R2-8a）**：每条必须含 `id`（非空且同计划内唯一——节点3 close_reason="scenario:<id>" 的可追溯性依赖，validate_report 第 41 项把关；缺 id=空名离场不可复盘）、`condition`、`level`、`action` 四要素（`rule_ref` 可选）。`level` 词表与节点3 执行通道对齐：`auto`=程序收盘条件自动执行（「N周期 收盘 OP 价」句式）、`semi`=AI 监督/人工确认通道（减仓/反手/收紧类）、`log`=仅观察记录或已由 exit_rules 七层机器化（条目作人读镜像）。**禁止**用 `scenario` 键内嵌序号替代独立 `id` 字段（work-20260902 实测交付漂移形态）。

