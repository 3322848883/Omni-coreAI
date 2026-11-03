# 三层架构：系统层 / 人格层 / 工具层

> 一句话：**系统层管「怎么下」，人格层管「下什么」，工具层管「看什么」。**
> 换策略只改人格；换交易所/加指令只改系统；加指标/技能只改工具层。
> 三者**不交叉**——交叉了就会「新建一个 bot 就报一次错」。

---

## 1. 分工

| 层 | 负责 | 载体（代码/文件） | 不该出现 |
|---|---|---|---|
| **系统层** | 输出格式、**订单原语**、字段硬约束、执行闸门、台账对账、状态机（`position_state`） | `omnialpha/strategist/prompt.py::SYSTEM_PROMPT`（提示词）<br>`omnialpha/executor.py`（执行）<br>`omnialpha/ladder_ledger.py`（台账）<br>`omnialpha/reconcile.py`（对账） | 任何具体策略参数（点位、层数、结构位、择时） |
| **人格层** | 下什么单、哪个方向、多少、什么价、什么条件、什么情况不进场 | `prompts/<persona>.md`（拼在 `【策略人格】` 之后） | 引擎怎么下单、触发单参数怎么拼、交易所报错码怎么绕 |
| **工具层** | 看什么：K 线 / 指标 / SMC / 挤压动量 / 技能目录 | `omnialpha/strategist/tools*.py`、`skills/`、`build_system_prompt(..., tools_guide, skill_catalog)` | 决策规则本身（「什么形态该进场」属于人格） |

拼装顺序（`build_system_prompt`）= 系统契约 → 工具指南 → 技能目录 → **【策略人格】**。
顺序即分层：越靠前越不可被策略改动。

---

## 2. 系统层：交易所订单原语（**全部就这 6 类**）

任何策略意图都必须落到下面某一类，否则计划在引擎侧被拒（不是被"修正"）。

| # | 原语 | 表达方式 | 关键约束 |
|---|---|---|---|
| ① | 市价单 | `type=market` | `price` 必须 null |
| ② | 限价单 | `type=limit` + `price` | 多头 `price<mark`、空头 `price>mark`，否则立即成交或被拒 |
| ③ | 突破进场 | `action=stop_entry_long\|stop_entry_short` + `trigger_price` | 缺 `trigger_price` 整条拒 |
| ④ | 保护单（条件触发） | `modify_tp_sl` 或开仓 chip 带 `tp/sl`（`tp_mode/sl_mode=trigger`） | 多头 `tp>mark` 且 `sl<mark`，空头反之；写反 → `AUTO_TRIGGER_PRICE_*` |
| ⑤ | 盘口 reduce_only 平仓 | `tp_mode/sl_mode=limit_order` | 只减仓、永不反手 |
| ⑥ | 撤单 / 平仓 / 减仓 | `cancel_all` / `cancel_price_all` / `close` / `close_all` / `flatten` / `reduce_*` | 按 `position_state` 决定可不可发 |

**字段硬约束**（缺一个 = 整条被拒）：

- **量**：`size_usd` / `size_coins` / `margin_usd+leverage` / `size`（合约张数）四选一、不可冲突；
  张数必须是 `size_step` 的整数倍（不足 floor，可能 floor 成 0 → 拒单）。
- **价**：必须落在 `order_price_round` 的 tick 上。
- **名义**：单笔 ≤ 实时权益 × `max_notional_pct`（`_check_notional`）。
- **张数读写两侧都禁 `int()`**：用 `executor.qty()` / `gate_client._f()`。

代码闸门（系统层真正的"接收器"）：

| 检查 | 位置 |
|---|---|
| 尺寸四口径 / 冲突拒单 / 步长量化 | `Executor._resolve_order_size` |
| 触发价方向预检 | `Executor._precheck_exit_triggers` |
| 单笔名义上限 | `Executor._check_notional` |
| wire 层（`price_orders.initial.size` 纯 int64） | `gate_client._wire_body` / `_int_price_order_size` |
| 状态机（`flat`/`entry_pending`/`position_open`/`unknown`） | `strategist` 快照 + 规则 16/17 |

---

## 3. 系统层：阶梯台账（2026-11-04 起）

`omnialpha/ladder_ledger.py` 是**所有梯类策略共用的唯一真相源**，交易所是权威、
台账是缓存。三条铁律：

1. **铺单成功必须回写 `order_id`**——否则无法判成交、无法感知撤单。
2. **成交档永不被重建删除**——它的 `tp_target` 是持仓止盈的唯一来源。
3. **层数预算跟着「持仓」走**——上限 N（= 一代梯子首次铺出的层数）：
   - **成交一档 → 占额度**（N−k 档可挂），新档从**下一层**开始铺（按最新价格），
     绝不原地补满（否则永远 N 层未成交，价格来回就无限重入）；
   - **止盈落袋（套利完成）→ 释放一档**（额度还回 N），该档下一轮**重新开档**
     （同价 `status: closed → pending`）。网格的定义就是「买低卖高再来一次」，
     把 closed 当永久消耗会让梯子越跑越稀。

字段：`entry_state`（open/filled，管预算）× `status`（pending/protected/filled/
closed/archived，管止盈），两者正交。

保护单张数对账（`reconcile.reconcile_exit_sizes`，300s 扫描）：

- **TP 目标 = 该侧持仓**（多一张都没有对应敞口）
- **SL 目标 = 该侧持仓 + 待成交入场**（风控要覆盖未来会成交的档）
- 只裁超额、绝不裁到目标以下；先挂后撤；取数失败不动（fail-closed）

---

## 4. 加东西时改哪一层

| 想做的事 | 改哪里 | 不要动 |
|---|---|---|
| 新策略（马丁/网格/PA/任何人格） | 新增 `prompts/<name>.md` + `config/bots/<bot>.yaml` | 引擎代码 |
| 调整某策略的层数/张数/结构位口径 | 人格 md 或 bot yaml | 系统提示词 |
| 新增一个指标/技能 | 工具层（`tools*.py` / `skills/`） | 人格与引擎 |
| 新增一种**下单方式**（如冰山/TWAP） | 系统层：`executor` 原语 + `prompt.py` 执行契约表 + `schema.CHIP_ACTIONS` | 各人格（它们只会写到已有原语） |
| 新增交易所/换 gateway | `gate_client` + wire 层 | 人格 |

> **判据**：如果一处改动只对某一个 bot 有意义 → 属于人格/配置；
> 如果换一个 bot 也需要 → 属于系统层。

---

## 5. 护栏（改坏了会被测试抓住）

- `tests/test_prompt_layers.py`：执行契约必须在系统层、6 类原语齐全、
  契约点名的动作都在 `CHIP_ACTIONS` 里、人格不许混进系统头、系统层不许出现策略参数。
- `tests/test_prompt_contract.py`：19 条规则齐全且每条带归属标签。
- `tests/test_ladder_ledger.py`：台账三条病根 + 保护单张数对账。
- `tests/test_levels_floor_tp_units.py` / `test_martin_pool_snap.py`：档位/TP/底仓。
