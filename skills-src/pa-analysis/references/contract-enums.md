# 契约枚举（交付形态的取值域）

> 与执行侧枚举常量逐字一致的取值域。用于自检方案字段是否落在合法域内 —— **写出域外的名字，执行侧一律判 manual（等于没触发）**。
> 63 条 `sequence` 词表另见 `sequence-vocabulary.md`；`signal_bar` 形态见下表。


## 计划顶层字段（`TOP_FIELDS`）

共 11 项：

- `account_gate`
- `combination_rules`
- `indicators`
- `key_levels`
- `market_context`
- `meta`
- `plan_handover`
- `plan_traceability`
- `plans`
- `risk_control`
- `scenario_matrix`


## 信号棒形态（`SIGNAL_BAR_PATTERNS`）

共 11 项：

- `bear_bar`
- `bull_bar`
- `doji`
- `engulfing`
- `hammer`
- `inside`
- `outside`
- `reversal_bar`
- `shooting_star`
- `strong_wide_range`
- `trend_bar`


## 交易生命周期阶段（`LIFECYCLE_STAGES`）

共 5 项：

- `after_stopout`
- `invalidation_handling`
- `post_entry_scenarios`
- `pre_trigger`
- `trigger_confirmation`


## 形态条件参数类型（`FORM_PARAM_TYPES`）

共 5 项：

- `market_state`
- `momentum_check`
- `risk_reward_check`
- `sequence`
- `signal_bar`

