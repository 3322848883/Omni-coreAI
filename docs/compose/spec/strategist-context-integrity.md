---
feature: strategist-context-integrity
status: in-progress
updated: 2026-10-06
branch: master
commits:
---

# 策略上下文完整性修复

让 LLM 策略层「看得见该看见的、记得住该记住的」。本批次修的是**既有设计与实现之间的落差**，
不是新功能设计——`agent-memory.md` 定义了记忆系统应该长什么样，本文档记录它实际没接上的地方。

## Report

## [S1] Problem

证据来源：服务器 `69.12.85.185:/opt/omnialpha`，bot `brooks-btc`，HEAD `47e3a65`，实测时间 2026-10-06。
所有数字均为运行时实测，非推断。

### P1 `memory_refs` 在解析层被丢弃

模型每轮都在输出这个字段，实测原文（cycle 213）：

```json
"memory_refs": ["btc-pa-15m-20261006-212","btc-pa-15m-20261006-211","btc-pa-15m-20261006-210"]
```

但 `schema.py:148` 的 `Plan` 没有该字段，`parse_plan`（`schema.py:288-295`）只取
`cycle_id` / `reasoning` / `chips` / `triggers` / `trigger_ops`。字段进入 `plan.raw` 后即被遗忘，
`loop.py:415` 的 `MemoryJournal.append()` 也就无从传参。

实测结果：`memory_journal.jsonl` **896 条记录，`memory_refs` 非空 0 条**（覆盖 148.6 小时）。

同时 `memory/context.py:71` 的 prompt 仍在要求模型「请输出 Plan JSON（含 memory_refs 引用历史决策）」。

### P2 实盘 bot 的画像恒为空

`data/bots/brooks-btc/state/memory_profile.json` **不存在**；`data/bots/brooks-btc/paper/` **不存在**。

`MemoryProfile.ledger_stats()`（`profile.py:31-58`）只认 paper 账本，无账本返回 `None`；
回退路径 `record_trade()` 在全仓库**唯一调用者是 `persona/runner.py:570`**，单 bot 的 `plan-loop`
路径没有平仓钩子。`profile.py:134` 的 `prompt_summary()` 因此在 `total_trades == 0` 时返回空串，
system prompt 里**没有「历史表现」这一段**。

`profile.py:34-46` 的 docstring 已经写明这个缺口，但它选的解法（改读 paper 账本投影）对 live bot 无效。

### P3 近况窗口只有 30 字

`context.py:51` 取 `journal.read_recent(3)`，`context.py:55` 把每条压成 `reasoning[:30]`。
实测 `[近况]` 段共 **171 字符**，`[上轮方案状态]` 段 **73 字符**。

模型每轮产出 6,286–76,211 字符的推理（实测区间），下一轮只回看到 30 字截断。
`journal.py` 写入侧实际存的是 `[:200]`，是 `context.py` 又砍了一刀。

### P4 挂单类型不可辨，模型靠猜

`snapshot.py:304-312` 的 `protections` 每条只取 6 个字段：

```python
{"contract", "id", "size", "trigger_price", "rule", "text", "status"}
```

`stop_entry_*` 是通过 `executor.py:1699` 的 `place_price_order` 下的**条件单**，因此它与 TP/SL
**混在同一个 `protections` 数组里**。Gate 的 `list_price_orders()` 返回里本来就有
`is_reduce_only` / `order_type` / `direction` / `is_stop_order`（实测字段清单已确认），只是没取。

后果：模型在 cycle 211 的推理里花了大段文字猜「85850 是 stop 还是 limit」——
而 `trades.jsonl` 里 `"order_type": "limit"` 写得清清楚楚。

同类问题 `snapshot.py:301` 的注释已记录过一次（「实测 2026-10-04 模型自己指出
『their trigger prices are null in the data』」），这是同一模式的第二例。

### P5 人格条款与系统文案直接冲突，导致撤挂振荡

`snapshot.py:94-99` 的 `position_state` 在 `entry_pending` 状态下明确告知模型：

> protections 里的单是随入场单预挂的，成交后才成为该持仓的保护

而 `prompts/brooks_btc_pa.md:25` 要求：

> **【孤儿保护单，必须处理】没有持仓，但存在止盈/止损类挂单**…**本轮必须撤销它们**

`entry_pending` 恰好就是「没有持仓 + 有保护单」。**两条规则互相矛盾**，模型选了语气更强的人格条款。

实测后果（`memory_journal.jsonl`）：cycle 216、217 连续两轮
`cancel_price_all,stop_entry_long`（相隔 1 分钟），撤单→重挂→再撤→再挂。
每轮成本 115,373 prompt tokens。

频次修正：200 轮里 `cancel_price_all` 8 次、`hold` 167 次（83.5%）。
**这是阶段性的（行情突破期集中在 212–218 七轮），不是持续模式**——但每次振荡的代价很高。

### P6 重复指令

实测 user prompt 末尾：

```
请输出本轮 Plan JSON。

请输出 Plan JSON（含 memory_refs 引用历史决策）。
```

分别来自 `prompt.py:209` 与 `context.py:71`。`loop.py:266-273` 把 `build_user_prompt` 的结果
作为 `snapshot_text` 塞进 `build_context`，两条路的收尾句都保留了。

## [S2] Design

### S2.1 `memory_refs` 贯通到 journal

- `Plan` 增加字段 `memory_refs: list = field(default_factory=list)`。
- `parse_plan` 取值：`data.get("memory_refs")`，非 list 一律降级为 `[]`（不抛 `PlanError`——
  这是观测性字段，不该让格式瑕疵毁掉整轮决策）。
- `loop.py:415` 的 `append()` 增加 `memory_refs=plan.memory_refs`。
- 元素规范化：只保留 `str`，strip 后非空的，最多 20 条（防模型灌入超长数组）。

契约：`memory_journal.jsonl` 的新记录该字段非空，且元素形如 `btc-pa-15m-20261006-213`。

### S2.2 近况摘要与索引

`[近况]` 段改为两层：

1. **摘要层**：最近 3 轮（`n_recent=3`），每轮取模型自写的摘要（见下），不截断。
2. **索引层**：最近 20 轮，每轮一行 `cycle_id decision`（不含推理文本）。

摘要来源：直接用 `plan.reasoning`，**不引入新字段约定**——模型已经在写这个字段，长度也合适
（实测 cycle 211 为 70 字符：「区域=趋势多头。15m放量突破86593创新高，1h/4h价在上升EMA20上；
等85850回踩多单、SL85550未失效，维持不动。」）。`journal.py:47` 已存 `[:500]`，
`context.py` 去掉 `[:30]` 即可，无需改写入侧。

契约：`[近况]` 段体积上限 **~2,000 字符**（3 段摘要 + 20 行索引），且**不随历史增长**——
这是它替代「回看 N 轮」的关键。实测：journal 10 轮与 510 轮时该段体积差 <200 字符。

### S2.3 `journal_lookup` 工具

新增原生工具 `journal_lookup(cycle_id)`，返回该轮 journal 记录的 `decision` / `reasoning` /
`exec_result` / `memory_refs`（不含完整思维链——那不在 journal 里）。

- schema 加进 `NATIVE_TOOLS`（`tools.py:309` 附近）。
- dispatch 加进 `run_tool`（`tools.py:1353`，已有 `bot_root` / `bot_id` 参数，可直接定位
  `data/bots/<id>/state/memory_journal.jsonl`）。
- 不在 `META_TOOL_NAMES` 里 → 受 bot 的 `tools.allow/deny` 约束（合理：它是可选工具，不是元工具）。
- 找不到 `cycle_id` 时返回结构化错误，不抛异常。

契约：模型能按 S2.2 索引层给出的任意 `cycle_id` 取回该轮决策全文。

### S2.4 实盘盈亏投影

**数据源必须是交易所**，不能用本地日志：`paper/store.py:158-160` 的 docstring 已明确
「`fills.realised_pnl != 0` 即一笔平仓成交——**它包含交易所侧触发的平仓**（SL/TP），
而那类没有信号、不进 `logs/trades.jsonl`」。

实测确认这条限制是真的：`trades.jsonl` 339 行里只有 **3 条**含 `realized_pnl`；
`data/shared/receipts/` 83 条里只有 **4 条**。两者都只覆盖本地发起的平仓。
用它们投影会漏掉全部 SL/TP 平仓——而止损恰是负面样本，漏掉会系统性抬高胜率
（`profile.py:38-44` 记录过一次同类事故：459 笔真实平仓只记了 1 笔）。

方案分两层，与既有的「读时投影」哲学一致：

1. **拉取层**（需要 client）：新增 `gate_client.list_my_trades(contract=None, limit=100, from_id=None)`，
   走 `GET {FUTURES_API}/my_trades`。新增 `omnialpha/memory/exchange_pnl.py`：
   `sync_exchange_pnl(root, bot_id, client) -> dict` 按游标幂等摄取 `pnl != 0` 的成交，
   累积落 `state/exchange_pnl.json`（含 `cursor` 与 `fills` 列表）。由 `watcher` 的 300s sweep 调用
   （挂在 `watcher.py:707-727` 的 `do_sweep` 块内，与 `_reconcile_sweep` 并列）。
2. **投影层**（纯读）：`MemoryProfile.ledger_stats()` 增加第三数据源——paper 账本 → `exchange_pnl.json`
   → 文件累加值。返回形状与 `realized_pnl_stats` 对齐：
   `{"trades", "wins", "pnl", "worst"}`。

错误行为：`my_trades` 拉取失败只记日志、不动游标（下轮重试）；`exchange_pnl.json` 缺失时
`ledger_stats()` 正常返回 `None`，不影响其他路径。

契约：`memory_profile.json` 的 `total_trades` 等于交易所平仓笔数（含 SL/TP 触发），
且 `prompt_summary()` 非空进入 system prompt。

### S2.5 挂单字段补全

`snapshot.py` 两处补字段（**只加不删**，不动任何现有字段）：

- `protections`（`:304`）增加：`is_reduce_only`、`direction`、`order_type`。
  - 取值兼容两种形状（Gate 嵌在 `initial` / paper 扁平），沿用现有 `_first()` 模式。
- `open_orders`（`:277`）增加：`tif`、`is_reduce_only`。

另在 `position_state` 的 `entry_pending` 文案里，把「protections 里的单是随入场单预挂的」
补一句判据：**`is_reduce_only: true` 的才是预挂保护，`false` 的是入场条件单**。

契约：模型能从快照直接判断任一挂单是「限价/市价入场单」「入场条件单」还是「保护单」，
无需再从 `text` 后缀猜。

### S2.6 人格第 25 条限定范围

`prompts/brooks_btc_pa.md:25` 的孤儿单条款增加前提：

- 明确「**孤儿单**指：**没有任何未成交入场单**，却存在 reduce-only 的 TP/SL 残留」。
- 明确「`entry_pending` 状态下随入场单预挂的保护单**不是**孤儿单，不得撤」。
- 撤单判据改为按 S2.5 的新字段（`is_reduce_only`），不再靠 `text` 猜。

契约：人格条款与 `snapshot.py:94-99` 的系统文案不再冲突。
这一条**依赖 S2.5**——没有字段，人格即使写了限定也无法执行。

### S2.7 指令去重

把 memory_refs 要求**合并进 `prompt.py:209`**（`请输出本轮 Plan JSON（含 memory_refs 引用历史决策）。`），
`context.py` 的模板改为**条件兜底**：只在 `snapshot_text` 为空时才补收尾句。

判据：`loop.py:325` 与 `loop.py:600` 两条路径（单 bot / 多人格）都先用 `build_user_prompt`
生成 user——它已含 `prompt.py:209` 的收尾句——再经 `_assemble_prompt` → `build_context`
包装（`:334` / `:610`）。所以正常路径只有一句；`build_context` 抛异常时的兜底分支
（`loop.py:277`）拿到的仍是 `build_user_prompt` 的输出，收尾句不丢。

**兜底判据用「调用方有没有给渲染好的快照」，不用子串探测**（`"Plan JSON" not in ...`）：
后者在快照正文恰好含这几个字时会静默吞掉兜底收尾句（独立评审实测过该路径：
`snapshot_text=""` + `snapshot={"market": {"note": "Plan JSON"}}` → 0 个收尾句）。

## [S3] Out of Scope

- **快照瘦身**（`candles: 120` / `extra_candles: 80`）。实测工具 tf 分布为
  15m 96.8% / 5m 1.8% / 1h 1.4% / 4h 0%，多周期逐根 K 线的边际价值可疑；
  但用户明确决定本轮不动快照粒度，留作独立评估。
- **强制重试判据**（`loop.py:1081`）。实测 `forcing a data-fetch retry` 仅
  2 次 / 792 轮（0.25%），不构成问题，本轮不改。
- **35 个工具 schema 收窄**。`skill` 工具调用已在 2026-10-06 13:50 后归零
  （配置改 `skills: []` 生效），问题已缓解。
- **`fork#4` 归属的 `schema.py` / `loop.py`**：本批次会改这两个文件（S2.1、S2.3 dispatch 在
  `tools.py`）。提交前须核对工作区，`omnialpha/memory/context.py` 有一处 fork#4 的未提交改动
  （删掉 context 后缀的括号说明），不得一并提交。

## Tasks

- [x] T1: 补全挂单字段 — acceptance: 快照的 `protections` 每条含 `is_reduce_only`/`direction`/`order_type`，`open_orders` 每条含 `tif`/`is_reduce_only`；Gate 与 paper 两种形状取值都正确 (covers: S2.5)
- [x] T2: 人格第 25 条限定孤儿单范围 — acceptance: `prompts/brooks_btc_pa.md` 的孤儿单条款明确排除 `entry_pending` 预挂保护单，且撤单判据用 `is_reduce_only` (covers: S2.6; depends: T1)
- [x] T3: 指令去重 — acceptance: user prompt 末尾只出现一次收尾指令；`build_context` 异常兜底路径仍有收尾句 (covers: S2.7)
- [x] T4: `memory_refs` 贯通 — acceptance: 新写入的 `memory_journal.jsonl` 记录 `memory_refs` 非空且元素为 cycle_id 字符串；非 list 输入降级为 `[]` 不抛异常 (covers: S2.1)
- [x] T5: 近况摘要与索引 — acceptance: `[近况]` 段含最近 3 轮摘要（不再 `[:30]` 截断）+ 20 行索引（含 `journal_lookup` 提示）；该段体积 ≤2,000 字符且 journal 从 10 轮涨到 510 轮时体积差 <200 字符；`[上轮方案状态]` 仍在 (covers: S2.2; depends: T4)
- [x] T6: `journal_lookup` 工具 — acceptance: 模型可按索引层的 cycle_id 取回该轮 decision/reasoning/exec_result；未知 cycle_id 返回结构化错误 (covers: S2.3; depends: T5)
- [ ] T7: 交易所盈亏拉取 — acceptance: `gate_client.list_my_trades` 可拉取成交；`sync_exchange_pnl` 幂等（重复调用不重复计入）；拉取失败不动游标 (covers: S2.4)
- [ ] T8: 画像接入交易所投影 — acceptance: `ledger_stats()` 在无 paper 账本时回退到 `exchange_pnl.json`，返回形状与 `realized_pnl_stats` 一致 (covers: S2.4; depends: T7)
- [ ] T9: 端到端验证 — acceptance: 服务器部署后，四项均可从落盘产物观察到：(a) `memory_journal.jsonl` 新记录 `memory_refs` 非空；(b) `memory_profile.json` 出现且 `total_trades` 与交易所平仓笔数一致；(c) 连续 5 轮的 `[近况]` 段体积 ≤2,000 字符且含 20 行索引；(d) 某轮 `*.thinking.json` 的 `tool_usage` 出现 `journal_lookup` 且其 `result_len > 0` (covers: S2.1 S2.2 S2.3 S2.4 S2.5; depends: T1 T2 T3 T4 T5 T6 T7 T8)
