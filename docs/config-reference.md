# Bot 配置键参考

> ⚙️ **本文件自动生成，请勿手改。**
> 来源：`omnialpha/config_keys.py`；生成：`python scripts/gen_config_reference.py`。
> `tests/test_config_reference_fresh.py` 会检测漂移。

白名单的用途：加载 bot 配置时，**白名单之外的键会告警**（阶段 1 只 WARN、不报错）。
原因是「配了但没人读」的键此前**完全静默** —— 实测先例：5 个 `ab-*` bot 配了
`account_risk.auto_protect: "dry"` 却没有 `auto_protect_sl_pct`，而
`ensure_protection` 在 `pct <= 0` 时直接早退，dry 观察期其实什么都没观察到。

启用 bot 请写 `config/bots.local/<同名>.yaml`（基线里的 `enabled` 一律忽略）。

---

## `<顶层>`

Bot 配置文件顶层（`config/bots/<id>.yaml`；启用走 `config/bots.local/`）

共 **31** 个键：

- `account`
- `account_risk`
- `account_scope`
- `accounts`
- `allow_close`
- `allow_reduce`
- `api_key_env`
- `api_secret_env`
- `bot_id`
- `default_replace`
- `enabled`
- `env`
- `exchange`
- `groups`
- `label_prefix`
- `manage_positions`
- `max_files_per_run`
- `max_notional_usd`
- `max_orders_per_file`
- `max_scan_symbols`
- `order_scope`
- `paper`
- `place_before_cancel`
- `poll_interval_sec`
- `position_policy`
- `require_sl`
- `root`
- `strategist`
- `symbols`
- `symbols_unrestricted`
- `validate_contracts`

## `account_risk`

风控闸门（可与 `config/accounts.yaml` 的账户级值合并，上限取更严）

共 **19** 个键：

- `auto_protect`
- `auto_protect_sl_pct`
- `daily_loss_limit_usd`
- `equity_deviation_halt`
- `equity_deviation_halt_pct`
- `give_back`
- `halt`
- `max_leverage`
- `max_notional_pct`
- `max_notional_usd`
- `max_total_notional_pct`
- `max_total_notional_usd`
- `no_flip`
- `peak_trail`
- `peak_trail_atr`
- `risk_pct`
- `risk_per_trade_pct`
- `safe_mode_after_failures`
- `total_risk_pct`

## `paper`

模拟盘参数（env: paper 时生效）

共 **14** 个键：

- `fee_rate`
- `feed_env`
- `feed_exchange`
- `funding_enabled`
- `initial_capital`
- `leverage`
- `maintenance_margin_rate`
- `maker_fee_rate`
- `margin_mode`
- `position_mode`
- `price_band_pct`
- `store_path`
- `tick_interval_sec`
- `trigger_price_type`

## `strategist`

策略引擎段（不写 = 该 bot 只跑外部信号）

共 **26** 个键：

- `account_risk`
- `ai_triggers`
- `bot_id`
- `bot_root`
- `candles`
- `check_interval_sec`
- `cond_gap_sec`
- `conditions`
- `enabled`
- `env`
- `event_on_kline_close`
- `event_timeframe`
- `interval_sec`
- `llm`
- `manage_positions`
- `market`
- `prompt_file`
- `risk`
- `skills`
- `skills_required`
- `symbols`
- `timeframe`
- `tools`
- `vision`
- `vision_timeframes`
- `write_hold`

## `strategist.ai_triggers`

AI 自设触发器策略（上限、冷却、TTL）

共 **6** 个键：

- `allow_modify`
- `allow_types`
- `default_cooldown_sec`
- `default_ttl_sec`
- `enabled`
- `max_active`

## `strategist.llm`

LLM 供应商与采样参数（覆盖 `config/providers.yaml` 的默认值）

共 **15** 个键：

- `api_key_env`
- `base_url`
- `base_url_env`
- `default_base_url`
- `fallback`
- `json_mode`
- `max_tokens`
- `model`
- `provider`
- `reasoning_effort`
- `temperature`
- `thinking`
- `timeout_sec`
- `user_id`
- `user_id_prefix`

## `strategist.market`

行情数据源（rest / hybrid / local，K 线周期与指标）

共 **11** 个键：

- `db`
- `exchange`
- `extra_candles`
- `extra_timeframes`
- `health_url`
- `indicators`
- `mode`
- `pa_data_root`
- `refresh`
- `stale_factor`
- `timeframes`

## `strategist.risk`

计划层风控（min_confidence / max_chips / allow_actions …）

共 **12** 个键：

- `allow_actions`
- `drop_on_low_conf`
- `max_chips`
- `max_chips_per_symbol`
- `max_notional_pct`
- `max_notional_usd`
- `min_confidence`
- `per_symbol`
- `risk_pct`
- `risk_per_trade_pct`
- `symbols`
- `total_risk_pct`

## `strategist.tools`

原生工具开关与预算（enabled / native / allow / deny …）

共 **8** 个键：

- `allow`
- `deny`
- `enabled`
- `max_calls`
- `max_rounds`
- `max_tokens`
- `native`
- `timeout_sec`
