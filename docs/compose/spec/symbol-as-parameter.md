---
feature: symbol-as-parameter
status: delivered
updated: 2026-10-09
branch: feat/symbol-as-parameter
commits: 07ae1b0..ee9a1f3（22 commits / 82 文件 / +8913 −575）
---

# 币种作为可配置参数（可切换 / 可添加）

> 工作区：`.worktrees/symbol-as-parameter`（linked worktree，基于 `origin/master` @ `07ae1b0`）。
> 环境：需 `python -m omnialpha skill install skills-src/{pa-analysis,price-action-trading} --yes`
> （`skills/` 是 gitignore 的运行时目录，全新 worktree 里为空会让 14 项 skillkit 测试失败）。
> 基线：**2056 项 OK / 0 失败 / 1 skip**（worktree 内实测；主树那 1 项 `test_skill_sizes`
> 是本地生成行情数据导致的假阳性，worktree 里不出现）。

## Report

### What was built

币种从「设计常量」变成「可配置参数」：**可切换**（改配置即换标的）、**可添加**（新币
自动获得全套工具/触发器/记忆/守护，不需要改代码）。三层权责落地 —— 币种由**配置层**
声明、**程序层**一路传递并校验、**人格/契约层**只消费（不再写死币名）。

| 层 | 交付 |
|---|---|
| 解析与缺省 | `strategist/symbols.py` 唯一解析来源；单币自动补、多币/越界/未配置一律拒绝；别名（`coin`/`token`/`contract`）归一 |
| 工具 | 22 个需 symbol 的工具统一前置（`SYMBOL_TOOLS`）；aux 工具币名三形态归一；`sentiment` 缺 coin 即拒（原先返回**任意币**的情绪） |
| 触发器 | 按币设唤醒条件（`max_active_per_symbol`）；yaml 叶条件缺 symbol：单币自动补、多币启动报错 |
| 计划闸门 | plan 层宇宙校验、按币名额（`max_chips_per_symbol`）、快照按币分区 |
| 执行器 | 四个钝动作加显式 `scope`（空 symbol 不再静默放大到全账户）；`close_all` 归属判据 + `skipped_unattributed`；保护单同步/清理 fail-closed；回滚留痕 + 「已回滚」通知 |
| 守护 | 扫描集合 = `bot.symbols` ∪「有归属的合约」→ **删币后存量仓位仍受守护**；共享 client + 币数上限 + 覆盖留痕 |
| 记忆 | journal / Tier-1 / 近况、画像 / 盈亏 / 衰减、订单上下文**全部按币**；画像移出稳定前缀（缓存） |
| 审计 | `trades.symbol` 不再恒 NULL + `symbols_json` + 按币查询；`plans.symbols_json` |
| 数据源 | `fetch_aux.CONTRACTS` 从 watchlist 读；按币覆盖告警；Bitget/HL 真实合约元数据；per-venue 覆盖矩阵（三态） |
| 安全 | HL 撤单带正确 `coin`（原先硬编码 `BTC`）；paper 平一侧不裸另一侧 + 逐合约杠杆；`sizing` quanto 缺失硬拒（与 paper 同源）；`account_scope` 决定熔断归属 |
| 人格/契约 | 契约示例不再写死币名；修 3 处**错尺度**示例；人格/宇宙一致性告警 |

### Verification

- 全量 **2325 项 OK / 0 失败 / 1 skip**（worktree 内；每条提交后都跑过全量）
- 新增多币测试 **106 项**（8 个文件）
- **「不是假绿」实测**：临时删掉 `tier1_journal_fields` 的币维度后 4 项测试立刻失败
- **生产路径接线**：`test_dull_action_scope` 全部从 `execute_signal` 触发，不只单测 helper

### Journey log

1. **并行实施踩坑**：subagent 并发上限实测约 2，一次派 4 个全部被取消，留下 4 处半成品
   （其中一个停在编辑中间、`executor.py` 语法错误）。此后改为自己逐批实施。
2. **反断言测试扫到自己写的注释**：`TestNoHardcodedSymbolInDegradePaths` 因注释里出现
   `BTC_USDT`（在说明历史）而误报 → 改成只扫**字符串字面量**（用 `ast` 剥离 docstring）。
3. **两个真 bug 是收尾时发现的**：`parse_intent` 的 hold 分支**丢掉 symbol**（多币下
   `replace=symbol` 于是收不掉旧单）；`_resync_protectors` 是 fail-**open**（漏配
   `label_prefix` 时会撤掉别的 bot 的保护单）。
4. **「越界 symbol 唯一解时纠正」被否决**：spec 要求**拒绝**该 chip（保留 `corrected_from`
   留痕）—— 纠正会把「模型当时写的是哪个币」这个事实丢掉。
5. **I11 的两处有意例外**（均已在提交信息里标注）：契约示例占位符化（T18）、画像移出
   稳定前缀（T13）。其余单币路径都有逐字 golden 断言钉住。

## [S1] Problem

**币种在本仓是「设计常量」，不是「配置参数」。** 表现不是"多币种功能没做"，而是
**切换标的与新增标的这两件本该只是改配置的事，会静默失效**。

用户要的是：**币种可以任意切换、可以随时添加，且切换/添加之后系统行为仍然正确、可验证**。
现状做不到，具体分三类：

### P1. 切换/新增标的会**静默失效**（最危险的一类）

| 现象 | 证据 | 为什么不报错 |
|---|---|---|
| 新增币后 aux 类工具（`tech_analysis`/`coin_info`/`onchain`/`social`/`market_stats`…）返回空 | `pa-data-source/fetch_aux.py:87` `CONTRACTS` 是**硬编码列表**，注释却自称"与 watchlist 一致" | 查询命中 0 行 → 返回 `[]`，模型读成"该币没数据" |
| 新增币后监控仍报健康 | `aux_monitor.py:64-68` 只看 `count>0`；`data_monitor.py:14-39` 只跟踪"曾经更新过"的 key | 零数据的币**不在被跟踪集合里** |
| 删掉某币后，它上面的存量仓位/保护单**立刻脱离全部守护** | `watcher.py:464/496/542/617/707` 五处扫描全部 `for sym in bot.symbols` | 覆盖面由配置决定，**没有"交易所上本 bot 有归属的合约"这个维度** |
| `symbols: []` 时：**任意币可开仓 + 零守护** | `watcher.py:235/395` `bot.symbols or None` → `executor.py:173-175` 空列表 = **不限制** | 白名单"看起来生效"（`status` 打印 `[]`），实际是双向放开 |
| 模型漏写 symbol → 工具**静默返回另一个币的数据** | `tools.py:1655` `sym = … or ""` → `gate_client.py:211-217` `contract=` 空 → `/tickers` 全量取 `raw[0]`；`tools.py:2351-2366` `sentiment` 缺 coin 走"全表最新 N 行" | 返回体 `symbol` 是空串/无关值，**没有任何告警** |
| 人格/契约里的币种与宇宙不一致 → 无校验 | `prompt.py:13` 格式示例写死 `"symbol":"BTC_USDT"`；14 份人格写死币名与**价格尺度**（`brooks_eth_pa.md:61` 是 ETH 人格却用 84880 举例） | 文本与配置矛盾**不报错** |

### P2. 系统按「一个 bot 一个币」的假设工作

| 假设 | 证据 | 切换/添加后的后果 |
|---|---|---|
| 账户级单一 `position_state` | `snapshot.py:407/146-152` | 持 BTC 空 ETH 时，规则 16/17 允许对任意 chip 发管理动作（对 ETH 发 → `NO_POSITION` 白烧一轮）；反向：只有 ETH 有孤儿单而 BTC 有仓时，规则 14 **永不触发** → 孤儿单静默留存 |
| K 线收盘只看首币 | `loop.py:1253-1262`（且 `llm-strategist.md:119` 把它写成**规格**） | 其余币的 `kline_close` **永不唤醒** |
| 降级/讨论兜底写死首币 | `loop.py:519/572/892`、`persona/runner.py:655-665` | 降级轮把"没分析"伪装成对首币的判断；讨论结论被静默改币 |
| 名额与槽位全局共享 | `risk.py:42-48`（`max_chips` 跨币抢）、`trigger_store.py:239`（`max_active` 全局） | BTC 占满名额/槽位 → 其他币系统性出局或永远设不上唤醒 |
| 记忆/画像无 symbol 维度 | `journal.py:42-53`、`loop.py:474-499`（只取 `chips[0]`）、`profile.py:53-66`、`exchange_pnl.py:213-227`、`loop.py:229-233`（订单上下文取最新一张） | 历史表现/近况/Tier-1 字段/订单前提**跨币混算或丢失**，直接污染模型判断 |
| 计划层没有宇宙闸门 | `risk.py:51`、`schema.py:59-65` 只校验字符集 | 宇宙外 chip **静默进 inbox**，靠 executor 白名单兜底 → 白烧一轮 |

### P3. 正确性无法验证（缺可观测与守卫）

- `tool_usage_summary` 只按工具名（`loop.py:909-921`）→ 算不出"缺 symbol 率/越界率/每币取数次数"；
- `run_once` 不清 `tool_usage`（`loop.py:109/582/1342`）→ plan-loop **跨轮累加**，统计失真；
- `thinking.json` **没有 charts 字段**（`loop.py:932-943`）→ "这轮给模型发了哪几个币哪些周期的图"不可核对；
- `ledger.trades.symbol` 列**生产恒为 NULL**（`ledger.py:22` 建表有列，`tradelog.py:69-76` 从不传）；
- 测试基线固化单币：`test_profile_ledger_projection.py:24` 自造的 `fills` 表**连 `contract` 列都没有** → 多币改动会**绕过**测试而不是被拦住（假绿）。

### 为什么现在做

切换/新增标的**已经是正在发生的事**（`ladder-sol`/`eth-range` 等就是换标的的产物；并行会话今天还在加 ladder 变体）。
每换一次都要人工记住"要同时改 watchlist、fetch_aux、人格、配置、白名单……"，漏一处就是静默失效。
本仓反复记录的失效形态（"要求了但不消费"、"静默兜底成默认值"）在这里集中出现 **85 处**（§证据附录）。

### 证据来源

四路全量只读审计（数据源身份 / 策略层 / 执行守护 / 记忆观测），全量清单见 **§证据附录**：
`A-1…A-15`（15）、`B-1…B-20`（20）、`C-1…C-17`（17）、`D-1…D-33`（33）。
其中 **实盘安全级 8 条**在 §S2.3 的升级论证里逐条给出"防住方式"。

## [S2] Design

### S2.1 设计原则与不变量

**三层权责**（这是所有设计决策的根）：

| 层 | 该管 | 不该管 | 为什么 |
|---|---|---|---|
| ① 策略人格 `prompts/*.md` | 风格、规则、风险哲学、适配的**品种类型**（24/7 加密 vs 有交割时段的金银） | 具体币名、具体价格数字 | 币名影响实测很小（换标的 9/9 标的正确），而写死币名会让**人格库随币种数线性膨胀并漂移**（`brooks_eth_pa.md` 已是第二份 5204 字符副本） |
| ② 系统契约 `prompt.py` | 输出格式、硬边界、symbol 合法性 | 具体币名 | 契约是**所有 bot 共用**的；写死一个币会持续把模型往那个币引（`prompt.py:13`） |
| ③ 程序（配置/代码/数据模型） | **币种参数的唯一权威**：来源、传递、缺省、校验、记忆维度 | 硬编码默认币种 | 只有程序层知道"这个 bot 到底管哪些币"；它必须是唯一真相源，否则各层各自猜 |

一句话：**币种由配置声明，程序层负责把它一路传到底并校验，其余两层只消费它。**

**不变量（I1–I12）** —— 每条都必须有机械守卫（否则等于口号）：

| # | 不变量 | 守卫 | 现状 |
|---|---|---|---|
| I1 | **来源唯一**：币集只来自配置；代码里除测试/示例外不得出现具体币名字面量 | 源码级全仓扫描（T16）+ 启动校验（T4） | 工具层 ✅（T1）；其余 ❌（`hyperliquid.py:189`、`loop.py:519/572/892`、`runner.py:599/665`、`fetch_aux.py:87`、`orderflow_tools.py:27`） |
| I2 | **缺省必须来自参数**：唯一宇宙→自动补；否则拒绝；**不得来自常量** | `symbols.py` 规则测试 | 3/22 工具接入；7 行情 + 10 aux + orderflow 未接（T6） |
| I3 | **越界不可执行**，且在 **plan 层**前移校验 | executor 白名单测试 + plan 闸门测试 | 执行层 ✅；plan 层无闸门（T5） |
| I4 | **归属唯一**：按币的状态以 symbol 为键，读写同键 | 状态文件 key 结构测试 | 部分（`give_back`/`peak_trail` key=`symbol\|side` ✅；`pnl_snapshot`/`trades.symbol` ❌） |
| I5 | **维度不丢**：原始记录可按币还原；汇总可合并但**不得只存合并值** | 字段测试 + 审计脚本 | 大面积 ❌（D-1…D-33） |
| I6 | **失败可归因且不伪装**：拒绝/降级带 symbol 与原因 | 降级路径测试 | ❌（`loop.py:519/572`、风控文案不带 symbol） |
| I7 | **不静默跨币**：取不到 A 币不得退回 B 币；不得套用别币元数据/尺度 | 拒绝路径测试 + 尺度核对 | ❌（`ticker`、`sentiment`） |
| I8 | **一致性校验前置**：宇宙 ⊄ 白名单 / 币不在该所 / 无元数据 → **启动即报错** | 配置校验测试 | ❌（`config.py:225` 连归一都没有） |
| I9 | **可观测**：覆盖率 / 缺 symbol 率 / 越界率 / 降级率 / 按币 PnL / 随 N 成本 | `_symbol_audit.py` + harness | ❌（summary 无 symbol、图不落盘、`trades.symbol` 恒 NULL） |
| I10 | **成本有界**：随 N 增长的资源有上限与超限策略 | 上限测试 + 实测曲线 | ❌（无闸门） |
| I11 | **单币行为逐字不变**：prompt/决策路径/状态文件/日志与改动前一致 | 单币 golden（prompt/journal/画像逐字） | 部分（工具层已保证） |
| I12 | **按币互不干扰**：一个币失败不阻塞另一个币 | 部分失败推进测试 | 与 C-4 冲突（需显式取舍，见 S2.2-D4） |

### S2.2 关键决策与「为什么不选另一个」

**D1 缺省策略：唯一宇宙自动补，多币/越界/未配置一律拒绝（不猜）**
- 备选：默认取宇宙首个 + 提示。**否掉的理由**：那会让模型拿**错币的数据**继续推理，
  误导比多一次工具往返严重得多（`ticker` 今天就是这个形态，且**完全静默**）。
- 代价与补偿：多币下漏写 symbol 会多一次往返 → 错误文案带 `universe` 列表让模型一次改对；
  单币自动补保证实盘单币 bot 行为不变（I11）。

**D2 symbol 身份：收敛成单一 `resolve_symbol`（含紧凑写法）**
- 现状两份实现（`gate_client.py:469` / `kline_watcher.py:171`）且**都处理不了 `BTCUSDT`**
  （静默产出 `BTCUSDT_USDT`）。备选：各自修各自的 → 否，**同一条判据两处实现必然漂移**（本仓已多次踩到）。

**D3 校验前置：启动 fail-fast，而不是运行时才炸**
- 依据：本仓已记录的失效形态是"配置看起来生效、实际不生效"（§S1-P1 六条全是这个）。
  启动即报错是唯一能把这类问题变成**可见**的手段。

**D4 跨币原子性：保留"一轮 = 跨币原子事务"**
- 现状：任一币失败 → `break` + `_rollback_newly_placed` 撤掉**所有**本轮新挂的单（含其他币）。
- 备选：改成按币事务（每币独立回滚）。**否掉的理由**：那会**丢掉"整轮失败"这个安全语义** ——
  半成功的轮次留在场上更难回滚；且当前配置都是单币，收益不确定。
- 补偿：把"回滚了哪些币"写进 detail + 通知（现在只发"挂单成功"卡片，与最终状态矛盾）。

**D5 钝动作作用域：显式 `scope: symbol|bot|account`**
- 现状：`close_all`/`cancel_*` 在 `symbol=""` 时**静默放大到全账户**，且 `close_all` **无任何归属过滤**。
- 备选：保持"空 = 全账户"（兼容）。**否掉的理由**：这是"币种不是参数"的直接后果 ——
  动作的作用域不该由"参数是否为空"隐式决定。能力不丢：`scope: account` 显式保留全账户语义。

**D6 守护扫描驱动源：`bot.symbols` ∪ 交易所上本 bot 有归属的合约**
- 现状：只按 `bot.symbols` → **删币即让存量仓位脱离全部守护**（换币的核心场景）。
- 备选：要求用户"删币前先平仓"。**否掉的理由**：把正确性建立在人的记忆上，正是本方案要消灭的东西。

**D7 记忆维度：原始记录带 symbol，汇总视图可合并**
- 依据：`ledger.trades` 有 `symbol` 列却恒 NULL、`fills` 有 `contract` 列却被投影 SQL 丢掉、
  `pnl_snapshot` 只有合并值 —— 三处都是"**有维度不用**"，而一旦只存合并值就**不可恢复**。
- 备选：只在 prompt 汇总层分币。**否掉的理由**：原始记录不修，历史数据永远还原不出来。

**D8 名额/槽位：按币配额（`max_chips_per_symbol` / `max_active_per_symbol`）**
- 现状全局共享 → BTC 占满则其他币系统性出局（弱币永远轮不到）。
- 风险控制：缺省 `max_chips_per_symbol = 1`（保守），跨币总量仍受 `max_total_notional_pct` 约束。

**D9 解析加固：先修复再解析 + 废弃字段宽容**
- 依据：实测 18% 的轮次因"JSON 少一个括号 / `scenarios` 非对象"**整轮产出归零**。
- 备选：维持严格（"格式错就该作废"）。**否掉的理由**：这是**用整轮决策的代价去惩罚格式抖动**，
  而 `scenarios` 在契约里早已写明"不要写、无消费方" —— 一个无人消费的字段不该杀死整轮。

**D10 提示词：人格去币种 + 契约示例占位符**
- 依据：实测换标的 9/9 标的正确（人格币名影响小），但**写死币名的维护代价是真实的**
  （14 份人格 + 副本漂移 + 两处错尺度示例）。
- 边界：**不做大改**（不重写人格、不为每币写人格），只做示例符号化 + 加载时一致性告警。

**D11 兼容策略：单币路径逐字不变**
- 凡涉及 prompt/记忆/状态文件的改动，都以"单币 bot 输出逐字相同"为验收（golden 测试）。
  理由：实盘 `brooks-btc`、`ladder-eth` 都是单币；**本方案的收益不能以动摇在跑的策略为代价**。

### S2.3 升级论证（每项为什么是升级而不是降级）

判定口径：**能力（能不能做）· 正确性（做对没有）· 可观测（出事知不知道）· 成本** 四个维度里，
不允许任何一项净下降；凡收紧必配"能力不丢"的显式开关，凡涉及数据来源必**先补来源再收紧**。

| # | 改动 | 现状 | 改后 | 可能的降级风险 | 如何防住 | 净判定 |
|---|---|---|---|---|---|---|
| 1 | HL `cancel_order` 带 contract（`hyperliquid.py:189`） | 对非 BTC 撤单**打到 BTC** | 按币撤单 | HL 未启用，无回归面 | 测试钉住 mapper | **升级**（修掉错币操作） |
| 2 | `close_all` 补归属判据 + 显式 `scope` | 可平**别的 bot** 的仓 | 默认只平自己的 | 若有人依赖"平账户全部" → 能力消失 | 保留 `scope: account` 显式开关；现有配置全是 own | **升级**（默认更安全，能力以显式开关保留） |
| 3 | `symbols: []` 执行型 bot 启动报错 + 守护驱动源扩展 | 任意币可开仓 + 零守护 | 报错 / 覆盖"有归属的合约" | 依赖"不限制"的用法被拒；REST 调用上升 | 保留 `symbols_unrestricted: true` 显式开关；扫描加"每轮共享 client + 每 symbol 一次快照 + 上限告警" | **升级**（把静默失效变成显式；成本有闸门） |
| 4 | 工具缺省：拒绝而非默认首币（T1 已做 3 个，T6 铺开） | 静默拿错币数据 | 报错 + 宇宙列表 | 多币漏写多一次往返 | 单币自动补（实盘零影响）；错误文案可自我纠正 | **升级**（静默错误→显式错误） |
| 5 | `sizing` quanto 缺失 → raise（`sizing.py:54`） | 可能算出 **10000× 偏差**的仓 | 拒绝下单 + 告警 | ⚠️ **若该所本就拿不到 quanto（Bitget/HL 现为硬编码 1.0），收紧会变成"不能下单"= 真降级** | **顺序强制**：先让 Bitget/HL 拉真实 instruments（T4/T16），**再**收紧兜底；否则保留 `quanto` 兜底但打 `degraded` 告警 | **升级**（前提是顺序正确，已写进任务依赖） |
| 6 | 熔断粒度：无 `account:` 时自动归组 / 告警 | 一个 bot 熔断，同账户其他 bot 继续开仓 | 账户级熔断 | 激进 bot 触发会**停掉同账户其他 bot**（影响面变大） | 配置项 `account_scope: auto\|bot\|<name>`；**先 dry 一版**（只告警不写 halt），观察后再切真熔断 | **升级**（风险语义正确 + 渐进收紧） |
| 7 | paper 补 `(contract, side)` 与逐合约杠杆 | 双向策略在 paper 里平一侧**即裸另一侧**；杠杆账户级 | 与 live 语义一致 | paper 账本结构变化 | 只加列、旧行兼容；`cancel_reduce_only_price_orders` 加 `keep_side` | **升级**（paper 结论才可信） |
| 8 | 守护驱动源含"有归属的合约"（C-2） | 删币 → 存量仓位**脱离全部守护** | 一直守护 | REST 调用随币数上升 | 每轮共享 client + 每 symbol 一次快照 + `covered/skipped` 落盘 + 上限告警 | **升级**（安全性 > 少量成本） |
| 9 | journal/画像/近况/订单上下文加 symbol | 跨币混算、Tier-1 只取首币、订单上下文丢其余单 | 可按币还原 | prompt 内容变化 → 缓存前缀抖动、token 上升 | 单币输出**逐字不变**（golden）；分币摘要限 3 币；画像移出"稳定前缀"（这本身是省钱的） | **升级**（判断质量 + 成本） |
| 10 | `max_chips`/`max_active` 按币配额 | 弱币系统性出局 | 每币有配额 | 总敞口/触发槽可能变多 | `max_chips_per_symbol` 缺省 1；跨币总量仍受 `max_total_notional_pct` | **升级**（公平性） |
| 11 | 人格去币种 + 契约示例占位 | 14 份人格写死币名/尺度，副本漂移 | 币种无关人格 + 占位示例 | 失去"针对某币的定制示例" | 不做大改；人格仍可声明适配的**品种类型**；加载时一致性告警（不阻断） | **升级**（可维护性；实测币名影响小） |
| 12 | 解析加固（JSON 修复 + 废弃字段宽容） | 18% 轮次整轮归零 | 尽量解析成功 | 宽容可能放进"格式不规范的 plan" | 只修**结构**（括号/尾逗号），不改语义；`scenarios` 丢弃时留 notes 告警 | **升级**（纯增益） |
| 13 | 观测补全（`tool_usage` 清零 / `charts` 落盘 / summary 带 symbol / `trades.symbol`） | 统计失真、图不可审计 | 可按币统计 | 无 | 只加字段；清零是修正语义 | **升级**（纯增益） |
| 14 | 配置 fail-fast（归一/去重/宇宙⊆白名单/label_prefix 唯一/accounts 一致） | 到执行层才拒单、重名前缀可跨 bot 撤单 | 启动即报错 | 现有非法配置会被拦下 | 报错文案给出"改哪里"；对**未启用** bot 只告警不阻断 | **升级**（把静默漂移变成可见） |

**结构性保证（不是逐条运气）**：
1. **凡收紧，都留显式开关**（`symbols_unrestricted` / `scope: account` / `account_scope`）→ 能力不丢。
2. **凡涉及数据来源的收紧，先补来源再收紧**（quanto 那条是范例，已写成任务依赖）。
3. **凡影响 prompt/状态的改动，单币逐字不变**（I11 golden）。
4. **凡新增判据，都有机械守卫**（S2.6），防未来回退。

### S2.4 目标态设计（契约与接口）

**① symbol 身份与配置（T4）**
- 单一 `resolve_symbol`：合并两份实现；补「无下划线但以 quote 结尾」分支（`BTCUSDT → BTC_USDT`）；
  采集侧改为引用同一实现。
- 配置加载**一处**归一 + 去重 + 校验：宇宙 ⊆ 白名单、该币在该 `exchange` 上有合约、
  `label_prefix` 全局唯一、`symbols: []` 对执行型 bot 报错（除非 `symbols_unrestricted: true`）。
- `plan`/`plan-loop` 补 `accounts=` 合并，并断言"strategist 看到的 `account_risk` == executor 的"。

**② 工具层统一前置（T6）**
- `run_tool` 维护「需要 symbol 的工具集合」，统一 `resolve_symbol_arg(args, symbols, tool=name)`；
  拒绝即返回 `symbol_error_payload`（含 `universe` 与 `hint`）。
- `run_orderflow_tool` 加 `symbols=`；aux 10 个工具统一三形态归一（把 `tech_analysis:2154` 的兜底推广）；
  `sentiment` 缺 coin → 拒绝；`contract` 工具补 `min_notional_usd`；`account` 的 `symbols` 进 schema 并与宇宙求交。

**③ 快照按币分区 + plan 层闸门（T5）**
- `account.position_state` → `{symbol: state}`（旧字段保留一版做兼容，值 = "任一币有仓"）；
  `position_state_note` 同步按币；契约规则 14/16/17 改为**按 chip 的 symbol**取值。
- `account.positions_by_symbol` / `open_orders_by_symbol`（每行加 `in_universe`），
  把"忽略别人的单"从提示词自律**下沉为字段**。
- `apply_risk` 前加硬闸门 `chip.symbol ∉ universe → reject`（与 `symbols.py` 同源）；`_safe_symbol` 接受宇宙。

**④ 触发器按币（T7）**
- `max_active` → `max_active_per_symbol`（保留总上限）；yaml 叶条件缺 symbol：单币自动补、多币**启动报错**。

**⑤ 降级与兜底不猜（T3）**
- `_hold_plan`/`_hold_fallback`：多币 → **逐币一条 hold**；空宇宙 → 报错（不写 BTC）。
- 讨论契约加 `symbol` 字段 + 越界拒绝；`_default_symbol` 多币时返回空并由调用方拒绝。
- `persona/runner.py`：越界 symbol → **拒绝该 chip**（保留 `corrected_from` 留痕），不静默改币；`:599` 兜底删除。
- `_resolve_order_id` 按 symbol 建/复用单。

**⑥ 名额与预算按币（T8）**
- `max_chips_per_symbol`（缺省 1）+ 跨币总量仍受 `max_total_notional_pct`；
- `_prompt_risk` 支持 `per_symbol` 覆盖；
- persona 侧 `_apply_risk` 与 `risk.py::apply_risk` **合并为同一份**（消掉"截断 vs 拒绝"两种口径）。

**⑦ 守护扫描驱动源（T10）**
- 覆盖面 = `bot.symbols` ∪ 交易所上本 bot 有归属的合约（有仓，或有 `t-<prefix>` 挂单/条件单）；
  每次 sweep 落 `covered_symbols`/`skipped_symbols`/`reason`。
- 每轮共享 client + 每 symbol 一次持仓/挂单快照；扫描移出 inbox 主循环或分片；N 上限告警。

**⑧ 钝动作与归属（T9）**
- `close_all`/`cancel_all`/`cancel_price_all`/`cancel_trail_all` 增加 `scope: symbol|bot|account`
  （缺省 `symbol`；`symbol=""` 且未给 scope → 拒绝）。
- `close_all` 补归属判据；`_resync_protectors` 与 `_cleanup_orphan_protectors` 的 `label_prefix`
  处理统一 fail-closed；`replace=all` 要么真正实现要么删除（含死代码 `_apply_replace`）；
  `own_tag == "signal"` 不再静默退回整表；风控文案统一带 symbol。
- 跨币回滚落 `rolled_back_symbols`；notify 对已回滚的腿改发"已回滚"卡片。

**⑨ 解析与观测加固（T10/T12）**
- `repair_json()`（去围栏 → 去尾逗号 → 按栈补括号）；`scenarios` 非对象 → 丢弃 + notes；
- `run_once` 开头清 `tool_usage`；`tool_usage_summary` 加 `by_symbol`/`missing_symbol`/`out_of_universe`；
- `kline` 审计体加 symbol（多币落数组）；`charts` 元数据（`{symbol, timeframe, bytes}`）落 `thinking.json`。

**⑩ 数据模型（T11–T15）**
- journal：`symbols[]` + `decisions:[{symbol,action}]` + `tier1:{symbol:{...}}`（旧行读时兼容）。
- 近况/索引按 symbol 分组；**单币输出逐字不变**。
- 画像：`ledger_stats` 返回 per-symbol + 合计；`by_action` → `by_symbol_action`；
  paper `realized_pnl_stats` 补 `group by contract`；`exchange_pnl.totals` 加 `by_contract`；
  decay 记录带 symbol。
- 订单上下文按 symbol 各一张（≤3）；`list_open(group, symbol)`；单 bot 也写订单记录。
- ledger：`insert_trade` 补 symbol（多 symbol → 展开或 `symbols_json`）；`plans.symbols_json`；查询加 symbol。
- 缓存前缀：画像移出稳定前缀；`check_prefix` 分别 hash `system` 与 `user` 稳定头；`cache_stats` 记 `symbols`。

**⑪ 数据源与观测闭环（T16/T17）**
- `fetch_aux.py` 的 `CONTRACTS` 从 `watchlist.yaml` 读；`COIN_INFO/ONCHAIN` 显式映射 + 工具描述写明"仅支持 X"。
- `aux_monitor`/`data_monitor` 增加 **per-symbol 覆盖检查**（"某币零数据"要告警）。
- 声明 per-venue 覆盖矩阵（哪所哪些币哪些周期），bot 启动校验"我的币在该所可用"。
- 新建 `scripts/_symbol_audit.py`（覆盖率/缺 symbol 率/越界率/每币取数次数/图清单）；测试补多币断言。

**⑫ 人格与契约最小清理（T18）**
- 契约示例 → 占位符；默认人格去掉"单币单计划"；人格价格示例符号化；
  **修两处错尺度反例**（`brooks_eth_pa.md:61`、`ladder_t_pa_sol.md:101`）；加载时币名一致性告警。
- 文档：`llm-strategist.md` 把首币行为从"规格"降为"已知限制"，补一节「symbol 是运行时参数」；
  `agent-memory.md` 的 S2.4/S2.5 明确「记录与画像必须可按 symbol 还原」。

### S2.5 覆盖矩阵（层 × 维度，无空格）

维度：**来源 · 传递 · 缺省 · 校验 · 维度 · 隔离 · 观测 · 成本**；判定 `✅ / 🟡Tn / ➖理由`

| 层 | 来源 | 传递 | 缺省 | 校验 | 维度 | 隔离 | 观测 | 成本 |
|---|---|---|---|---|---|---|---|---|
| 配置 | 🟡T4 | 🟡T4 | ➖（无缺省概念） | 🟡T4 | ✅ | 🟡T4 | 🟡T4 | ✅ |
| 数据源身份 | ✅ | 🟡T4 | 🟡T6 | 🟡T4/T16 | 🟡T16 | 🟡T16 | 🟡T16 | 🟡T10 |
| 快照 | ✅ | ✅ | ✅ | ✅ | 🟡T5 | ✅ | ✅ | 🟡T10 |
| 契约 | ✅ | ✅ | ➖ | 🟡T5 | 🟡T5 | ✅ | ✅ | ✅ |
| 工具 | 🟡T6 | 🟡T6 | 🟡T6 | 🟡T6 | ✅ | ✅ | 🟡T10 | ✅ |
| 触发器 | ✅ | 🟡T7 | 🟡T7 | 🟡T7 | 🟡T7 | ✅ | 🟡T10 | ✅ |
| 计划/风控 | ✅ | ✅ | ✅ | 🟡T5 | 🟡T8 | ✅ | ✅ | ✅ |
| 执行 | ✅ | ✅ | 🟡T9 | ✅ | ✅ | 🟡T9 | 🟡T9 | 🟡T10 |
| 守护 | 🟡T10 | ✅ | ➖ | ✅ | ✅ | ✅ | 🟡T10 | 🟡T10 |
| 记忆 | ✅ | 🟡T11 | ➖ | 🟡T14 | 🟡T11–T13 | 🟡T14 | 🟡T16 | 🟡T13 |
| 观测 | ✅ | 🟡T10 | ➖ | ➖ | 🟡T10 | ✅ | 🟡T16 | 🟡T10 |
| 人格 | 🟡T18 | ✅ | ➖ | 🟡T18 | ➖（风格层无需按币） | ✅ | ➖ | ✅ |
| 文档/测试 | 🟡T18 | ➖ | ➖ | 🟡T17 | 🟡T17 | ➖ | 🟡T16 | ➖ |

**读法**：每格要么 `✅`、要么指向一个任务、要么 `➖` 且理由在 [S3]。**没有空白 = 没有未决项。**

### S2.6 回归守卫（"可拦"的具体形态）

1. **源码级**：全仓扫描 `omnialpha/**`、`pa-data-source/**`（除测试/示例/文档）禁止具体币名字面量，
   例外走显式白名单 + 理由。
2. **运行期**：`scripts/_symbol_audit.py` → 覆盖率 / 缺 symbol 率 / 越界率 / 自动补全率 / 每币取数次数 / 图清单；
   A/B harness 输出覆盖率与降级率。
3. **契约级**：单币 golden（prompt 逐字、journal 逐字、画像逐字）。
4. **配置级**：启动断言（宇宙 ⊆ 白名单、币在该所有合约、`label_prefix` 唯一、strategist 预算 == 闸门）。
5. **审计级**：每次改动跑 S2.8 矩阵。

### S2.7 成本与预算

**实测 token（本地 A/B，`_ab_multi_symbol.py`）**

| N（币数） | 旧人格 | 新人格 | 图数 |
|---|---|---|---|
| 1 | 65.9k | 77.8k | 4 |
| 2 | 111k | 119k | 8 |
| 3 | 153k | 154k | 12 |
| 5 | 214k | 229k | 20 |

→ **边际 ≈ 35–40k token/币**；图数 = `vision_timeframes × N`。按"被分析的币"摊销：
N=5 时旧人格 214k 买 1 个币、新人格 229k 买 5 个（差 4.6×）。

**代码侧推断（未实测，T10 补测）**：快照 ≈ 6 REST/币/轮；守护 ≈ `4×N` 账户请求/300s + `N`/60s；
开仓 `_check_account_risk` 每 intent 3×`get_account` + `get_positions` + 每持仓 `get_contract`。

**闸门**：图数上限（≤12 或"每币仅主周期+1"）、每轮 REST 上限告警、扫描移出主循环。

### S2.8 验收（测试矩阵）

| 维度 | 取值 |
|---|---|
| 币数 N | 1 / 2 / 3 / 5 |
| 标的 | BTC / ETH / SOL / XAU / XAG（本地有真实数据） |
| 场景 | 正常 / 缺数据 / 部分失败 / 越界 symbol / 缺 symbol / 交易所无此合约 / **换币（删旧币留存量仓）** |
| 断言 | 覆盖率 100%；标的正确率 100%；缺 symbol 率 0（多币）；降级率 0；**单币 prompt/journal/画像逐字不变**；成本随 N 有界；守护 `covered` 覆盖"有归属的合约" |

## [S3] Out of Scope

| 不做 | 理由 |
|---|---|
| 把跨币原子性改成"按币事务" | 会丢掉"整轮失败"的安全语义，半成功轮次更难回滚；当前配置都是单币（D4） |
| `exchange_pnl` 在**拉取层**按币分段 | `position_close` 无单调 id、游标全账户，分段会反复覆写游标（`exchange_pnl.py:374-378` 已论证）；分币只做在投影层 |
| `health.json` / heartbeat 加 symbol | 进程级健康，与币无关（不适用，非遗漏） |
| 按币独立风控预算池 | 先观察多币实盘行为；单笔上限已按权益比例动态收紧，跨币有 `max_total_notional_pct` |
| 为每个币写人格 / 重写人格 | 实测币名影响小（换标的 9/9）；副本应合并而非增殖 |
| 契约强制"每个币都必须有结论" | 属**策略选择**（scalp 型人格只盯一币是合法的），归人格层 |
| Bitget/Hyperliquid 完整适配 | 只做安全级（`cancel_order` 带 contract）+ 真实元数据；两所未启用 |
| 统一六所周期覆盖（补 30m） | 采集侧改动面大；改为**声明覆盖矩阵 + 启动校验**，防"以为有其实没有" |
| persona 多账户拓扑（`mirror_accounts`）与 symbol 的重设计 | 本方案只保证 symbol 维度正确，不改拓扑语义 |
| 改动实盘配置 / 服务器 / `pa-data-source` 采集频率与保留策略 | 全部改动先在本地验证；采集侧只改"品种来源与告警" |
| 修改降级 hold 的"保留投票权"语义 | 那是刻意设计（一个成员失败不该让它整轮缺席） |

## Tasks

- [x] T1: symbol 唯一解析来源 `strategist/symbols.py` + `run_tool(symbols=)` + 根除 6 处 `BTC_USDT` 兜底 + 6 处 schema 描述 — acceptance: `tests/test_tool_symbol_universe.py` 16 项通过（含源码级反断言）；全量 2056 项 OK (covers: S2.4②)
- [x] T2: `_kline_closed` 支持多币（任一币收盘即唤醒，触发标签带 symbol） — acceptance: 多币下"第二个币收盘"能唤醒（旧判据 monkeypatch 回去必失败），单币行为不变 (covers: S1-P2)
- [x] T3: 降级/讨论兜底不猜（多币逐币 hold、讨论契约加 symbol、`runner` 越界拒绝、`_resolve_order_id` 按币） — acceptance: 多币降级产出 N 条 hold 且 symbol 集合 == 宇宙；越界 chip 被拒且不静默改币 (covers: S2.4⑤)
- [x] T4: symbol 身份与配置 fail-fast（单一 `resolve_symbol` 含紧凑写法、配置归一/去重、宇宙⊆白名单、`label_prefix` 唯一、空 symbols 报错、`plan` 补 `accounts=`） — acceptance: 构造非法配置启动即报错并指出改哪里；`BTCUSDT` 归一为 `BTC_USDT` (covers: S2.4①; 支撑 I1/I8)
- [x] T5: 快照按币分区 + plan 层宇宙闸门（`position_state{symbol:…}`、`positions_by_symbol`、`apply_risk` 宇宙校验、规则 14/16/17 按币） — acceptance: 宇宙外 chip 在 plan 层被拒；持 BTC 空 ETH 时对 ETH 的管理动作不再被放行 (covers: S2.4③; depends: T4)
- [x] T6: 工具统一前置（7 行情 + 10 aux + orderflow 接入 `resolve_symbol_arg`；`sentiment` 缺 coin 拒绝；`contract` 补 `min_notional_usd`；`account.symbols` 进 schema 并与宇宙求交） — acceptance: 多币漏写 symbol → 拒绝且带宇宙；单币自动补；`ticker` 空符号不再返回任意币数据 (covers: S2.4②; depends: T4)
- [x] T7: 触发器按币（`max_active_per_symbol`；yaml 叶条件缺 symbol 单币自动补、多币启动报错） — acceptance: 5 币宇宙下每币都能设上唤醒条件；缺 symbol 的条件不再"永不触发" (covers: S2.4④; depends: T4)
- [x] T8: 名额与预算按币（`max_chips_per_symbol` 缺省 1；`_prompt_risk` 支持 per_symbol；persona 与 live 风控合并同一份） — acceptance: 一币多 chip 不再挤掉其他币；两处风控对同一 plan 结论一致 (covers: S2.4⑥)
- [x] T9: 钝动作 `scope` + 归属 + `replace=all` + 通知/文案 + 回滚留痕（含 `close_all` 归属判据、`_resync_protectors` fail-closed、删 `_apply_replace` 死代码、风控文案带 symbol、已回滚腿改发"已回滚"卡） — acceptance: `symbol=""` 未给 scope 被拒；`close_all` 不再平他 bot 的仓；回滚后通知与最终状态一致 (covers: S2.4⑧)
- [x] T10: 守护驱动源 + 观测与成本（覆盖面含"有归属的合约"、`covered/skipped` 落盘、`run_once` 清 `tool_usage`、`tool_usage_summary` 带 by_symbol、`kline` 带 symbol、`charts` 元数据落盘、扫描共享 client 与上限） — acceptance: 删币后其存量仓位仍被扫描覆盖；`thinking.json` 可按币统计工具与图 (covers: S2.4⑦⑨; depends: T9)
- [x] T11: journal / Tier-1 / 近况按币（`symbols[]`、`decisions[]`、`tier1{symbol:{…}}`、近况与索引按币渲染） — acceptance: 两币 → journal 两条独立可还原记录；**单币输出逐字不变**（golden） (covers: S2.4⑩)
- [x] T12: 解析加固（`repair_json`：去围栏/去尾逗号/按栈补括号；`scenarios` 非对象 → 丢弃 + notes） — acceptance: 少一个 `}`、尾逗号、`scenarios:"x"` 三种坏输入都能解析成功；降级率降为 0 (covers: S2.4⑨)
- [x] T13: 画像/盈亏/decay 按币 + 缓存前缀分层（`ledger_stats` per-symbol、paper `group by contract`、`exchange_pnl.by_contract`、decay 带 symbol、画像移出稳定前缀、`check_prefix` 分 hash） — acceptance: 两币各自独立胜率/盈亏；单币画像逐字不变 (covers: S2.4⑩; depends: T11)
- [x] T14: 订单上下文与共享订单库按币（按 symbol 各一张 ≤3、`list_open(group, symbol)`、`create` 校验宇宙、单 bot 也写订单记录） — acceptance: 多币多单时两张单的 `premise_invalidation` 都在 prompt 里；不再关错币的单 (covers: S2.4⑩; depends: T11)
- [x] T15: ledger 写 symbol（`insert_trade` 补 symbol / `symbols_json`、`plans.symbols_json`、查询加 symbol 参数） — acceptance: 新写入的 `trades.symbol` 非 NULL 且可按币查询 (covers: S2.4⑩)
- [x] T16: 数据源闭环（`fetch_aux.CONTRACTS` 从 watchlist 读、aux/data monitor per-symbol 覆盖告警、per-venue 覆盖矩阵 + 启动校验、Bitget/HL 真实元数据） — acceptance: 新增币后 aux 工具**不再静默空**（要么有数据、要么显式告警）；"某币零数据"能被监控报出 (covers: S2.4⑪)
- [x] T17: 测试补多币断言（`fills` 测试表补 `contract`、画像/盈亏/近况各加"两币→两条独立记录"、prompt 多币预算守卫） — acceptance: 故意把 symbol 维度删掉时新测试必须失败（不是假绿） (covers: S2.4⑪; depends: T11, T13)
- [x] T18: 人格与契约最小清理（示例占位符、默认人格、价格示例符号化、修两处错尺度反例、加载时一致性告警、文档口径） — acceptance: 契约示例不含具体币名；`brooks_eth_pa.md`/`ladder_t_pa_sol.md` 的错尺度示例已修；人格币名 ∉ 宇宙时告警 (covers: S2.4⑫)
- [x] T19: 安全批（HL `cancel_order` 带 contract；paper 补 `(contract, side)` 与逐合约杠杆；熔断 `account_scope` + 先 dry；空 `symbols` 报错） — acceptance: HL 对非 BTC 撤单带正确 coin；paper 平一侧不再撤另一侧的保护单；熔断 dry 一版只告警 (covers: S2.3 #1/#6/#7)
- [x] T20: `sizing` quanto 收紧（缺失即 raise，与 paper 语义一致） — acceptance: quanto 缺失时拒绝下单并告警；**依赖 T16 先补齐各所真实元数据**，否则不得开启 (covers: S2.3 #5; depends: T16)

## 证据附录（四路全量审计，一行一条）

> 原始逐条报告（含原文片段）在会话任务文件 `tasks/T330–T333/progress.md`。
> 分类：`硬编码` / `无传递路径` / `无维度` / `静默兜底` / `未校验` / `其他`；`⚠️` = 实盘安全级。

**A. 数据源与 symbol 身份**
| ID | 位置 | 问题 | 分类 | 阶段 |
|---|---|---|---|---|
| A-1 | `gate_client.py:469` / `kline_watcher.py:171` | 两份 `resolve_symbol`，语义不同，**都处理不了 `BTCUSDT`**（静默产出 `BTCUSDT_USDT`） | 硬编码+静默兜底 | T4 |
| A-2 | `config.py:225`、`__main__.py:64` | bot `symbols` 完全不归一/不校验（小写或裸写原样进快照） | 未校验 | T4 |
| A-3 | `exchanges/bitget.py:117`、`hyperliquid.py:113` | `get_contract` 整段硬编码 → 所有币共用精度/杠杆 | 硬编码 | T16 |
| A-4 | `exchanges/hyperliquid.py:189` | ⚠️ 撤单硬编码 `coin: "BTC"` → 非 BTC 撤单打到 BTC | 硬编码 | T19 |
| A-5 | `sizing.py:54` | `quanto_multiplier` 静默兜底 1.0（差 10000×），与 paper 的硬拒相反 | 静默兜底 | T20 |
| A-6 | `gate_client.py:37`、`tools.py:1712` | `ContractMeta` 无 `min_notional_usd`，`contract` 工具也不返回（描述却承诺） | 无维度 | T6 |
| A-7 | `tools.py:2061/2091/2124/2210/2240/2273` | aux 工具键形态不归一 → 命中 0 行**静默空**（仅 `tech_analysis:2154` 有三形态兜底） | 静默兜底 | T6 |
| A-8 | `tools.py:2351-2366` | ⚠️ `sentiment` 缺 coin → 查全表，返回**任意币**数据（schema 无 required） | 静默兜底 | T6 |
| A-9 | `fetch_aux.py:87/90/101/107` | 品种集合硬编码，与 watchlist 不同步 → 新增币 aux 静默空 | 硬编码 | T16 |
| A-10 | `aux_monitor.py:64`、`data_monitor.py:14` | 只看表级 `count>0` / 只跟踪"见过的 key" → **零数据的币永不告警** | 无维度 | T16 |
| A-11 | `watchlist.yaml:3` vs `watchlist_testnet.yaml:3`、`kline_watcher_multi.py:35` | live/testnet 品种集不同；Gate 有 30m 其余五所无 | 无维度 | T16 |
| A-12 | `exchanges/*` Mapper、`ws_venues.py:26-32`、`kline_watcher_multi.py` | 多所映射逻辑散落三处，漂移风险 | 其他 | T16 |
| A-13 | `ws_venues.py:299-316` | WS 回报归一不匹配时**静默丢 bar** | 静默兜底 | T16 |
| A-14 | `snapshot.py:268-271` | hybrid 降级只有 `degraded` 有痕、无告警 | 无维度 | T16 |
| A-15 | `kline_watcher.py:301-320`（采集侧有）/ bot 侧无 | bot 配置写不存在的币**启动不报错** | 未校验 | T4 |

**B. 策略层**
| ID | 位置 | 问题 | 分类 | 阶段 |
|---|---|---|---|---|
| B-1 | `snapshot.py:407/146-152` | ⚠️ `position_state` 账户级一个值；规则 14/16/17 依赖它 → 误放行管理动作 / 孤儿单静默留存 | 无维度 | T5 |
| B-2 | `snapshot.py:326-336/344` | 持仓/挂单账户级全量、无按币分区（保护单已按币 ✅） | 无维度 | T5 |
| B-3 | `prompt.py:210-219`、`loop.py:152-182` | 【策略风控】无 per-symbol 预算 | 无维度 | T8 |
| B-4 | `risk.py:42-48` | ⚠️ `max_chips` 跨币抢名额 → 弱币系统性出局 | 无维度 | T8 |
| B-5 | `risk.py:51`、`schema.py:59-65` | ⚠️ plan 层**无宇宙闸门**（只校验字符集）→ 宇宙外 chip 静默进 inbox | 未校验 | T5 |
| B-6 | `tools.py:1655` + `gate_client.py:211-217` | ⚠️ `ticker` 空符号 → `/tickers` 取 `raw[0]` 拿**任意币**数据（返回体 symbol 为空） | 静默兜底 | T6 |
| B-7 | `tools.py:1554/1598/1685/1715/1748/1795`、`:1548` | 7 个行情工具 + orderflow 未接宇宙校验 | 无传递路径 | T6 |
| B-8 | `tools.py:1884` vs `:598` | `account` 的 `symbols` 参数未声明、未校验 | 未校验 | T6 |
| B-9 | `orderflow_tools.py:27` | schema 示例写死 `BTC_USDT` | 硬编码 | T6 |
| B-10 | `trigger_store.py:239` | ⚠️ `max_active` 全局共享 → 币间互相饿死 | 无维度 | T7 |
| B-11 | `triggers.py:89/142-144` | ⚠️ yaml 条件缺 symbol → **永不触发**（静默） | 静默兜底 | T7 |
| B-12 | `loop.py:1253-1262` | K 线收盘只看首币（且文档写成"规格"） | 硬编码 | T2/T18 |
| B-13 | `loop.py:519/572` | 降级 hold 只覆盖首币 + 硬编码 `BTC_USDT` | 硬编码+静默兜底 | T3 |
| B-14 | `loop.py:778-790/879/892` | 讨论契约无 symbol；chip 兜底链以 `BTC_USDT` 收尾 → 多币讨论退化为单币 | 无维度+静默兜底 | T3 |
| B-15 | `persona/runner.py:655-665/588-599` | ⚠️ 越界/空 symbol **静默改成 `allowed[0]`** 并照常下单 | 静默兜底 | T3 |
| B-16 | `persona/runner.py:738-771` | persona 风控"截断" vs `risk.py`"拒绝"，口径不一且无 symbol | 无维度 | T8 |
| B-17 | `prompt.py:13`、`:171-176` | 契约示例写死 `BTC_USDT`；默认人格写"单币单计划" | 硬编码 | T18 |
| B-18 | `prompts/*.md`（14 份） | 人格系统性写死币名与价格尺度（含**两处错尺度**：`brooks_eth_pa.md:61`、`ladder_t_pa_sol.md:101`） | 硬编码 | T18 |
| B-19 | `prompts/brooks_multi_pa.md:18` | 说明已 stale（仍称"漏写会按默认币处理、不报错"） | 其他 | T18 |
| B-20 | `docs/compose/spec/llm-strategist.md:119` | 文档把"首币收盘"写成规格 | 其他 | T18 |

**C. 执行与守护层**
| ID | 位置 | 问题 | 分类 | 阶段 |
|---|---|---|---|---|
| C-1 | `watcher.py:235/395` + `executor.py:173-175` | ⚠️ `symbols: []` → 白名单变 None（**任意币可开仓**） | 静默兜底 | T19 |
| C-2 | `watcher.py:464/496/542/617/707` | ⚠️ 守护覆盖面 = `bot.symbols` → **删币即让存量仓位脱离全部守护** | 静默兜底 | T10 |
| C-3 | `executor.py:2859-2887/2889-2974/3033-3098/1813-1817` | ⚠️ 四个钝动作 `symbol=""` 静默放大到全账户；`close_all` **完全无归属过滤** | 未校验 | T9 |
| C-4 | `executor.py:254-261` + `336-409` | ⚠️ 一个币失败 → `break` + 回滚撤掉**其他币已挂的腿**（跨币原子性，需拍板） | 静默兜底 | T9（保留+留痕） |
| C-5 | `executor.py:272-278`、`642-673` | `replace=all` 与 `symbol` 行为相同；`_apply_replace` 死代码且无保护守卫 | 无维度 | T9 |
| C-6 | `executor.py:719-725/932-952`；`config.py:249-253`；`__main__.py:126/151/305` | ⚠️ 熔断逐 bot（仓库无 `accounts.yaml`）；`plan`/`plan-loop` 不合并 accounts → 预算比闸门松 | 无维度+无传递路径 | T19 |
| C-7 | `config.py:230` | ⚠️ `label_prefix` 无唯一性校验 → 重名即跨 bot 撤单/改单 | 未校验 | T4 |
| C-8 | `executor.py:2090-2094` vs `:2141` | ⚠️ 两处 `label_prefix` 处理不对称（空 prefix 时一个不动、一个全动） | 未校验 | T9 |
| C-9 | `executor.py:2895-2899/3057` | `own_tag == "signal"` 静默退回整表撤单 | 静默兜底 | T9 |
| C-10 | `paper/engine.py:411-412`、`store.py:456-479`、`exchange.py:111-125`、`store.py:121-128` | ⚠️ paper 三处分叉：平仓按 contract 撤单（丢 side）→ 双向策略裸另一侧；杠杆/保证金账户级；`pnl_snapshot` 无 contract | 无维度 | T19 |
| C-11 | `backtest/replay.py:14-19/44-46` | 回测无 symbol、基准单序列 → 多币混算 | 硬编码 | T15 |
| C-12 | `notify.py:696-700`、`watcher.py:249-262` | 多币 `close_all` 只发一张卡（币种被滤掉、无 per-coin PnL）；**已回滚的腿照样发"挂单成功"卡** | 无维度+静默兜底 | T9 |
| C-13 | `executor.py:980/924/835/853/1198/1226/1148` | 风控文案不带 symbol → 归档顶层 `error` 无法归因 | 无维度 | T9 |
| C-14 | `watcher.py:452/485/529/604/694` + 各 sweep | 扫描各建 client、每币重复查持仓/挂单、无缓存、同步阻塞 → 成本随 N×M 无界 | 无维度 | T10 |
| C-15 | `executor.py:2697/2472` vs `:2660-2680/2576-2582` | key=`symbol\|side` ✅，但"加仓改 entry_price"会**重置峰值**（推断，待实测） | 其他 | T10 |
| C-16 | 归属矩阵（见审计原文） | `_close`/`_close_all`/`_resync_protectors`/非 own 分支缺归属判据 | 未校验 | T9 |
| C-17 | `executor.py:411-537`、`reconcile.py:134-248`、`notify.py:639-750`、`paper/store.py:53-65` | **已改好**：按 symbol 分键、多腿、fail-closed、逐 step 出卡、paper 表结构按 contract | 其他 | — |

**D. 记忆与观测层**
| ID | 位置 | 问题 | 分类 | 阶段 |
|---|---|---|---|---|
| D-1 | `memory/journal.py:42-53` | journal 记录无 symbol 维度 | 无维度 | T11 |
| D-2 | `loop.py:414-417` | `decision` 逗号串 → 动作与币的对应在写入时丢失 | 无维度 | T11 |
| D-3 | `context.py:56-65/169-194`（`read_recent_summaries` 是死方法） | 近况/索引**不出现币名** → 直接污染判断 | 无维度 | T11 |
| D-4 | `loop.py:474-499` + `context.py:197-219` | `[上轮方案状态]` 只回看 `chips[0]` 且无币别 | 无维度 | T11 |
| D-5 | `context.py:41-46` + `profile.py:142-156` | 画像放在"稳定前缀"里 → **每平一笔就整段 system 缓存失效** | 其他（成本） | T13 |
| D-6 | `context.py:72-80/122-126` | `order_block`（含 symbol）是 user 第一段 → 换币/换单使 user 全段失效 | 其他（成本） | T13 |
| D-7 | `context.py:200-205` | 单 bot 从不写订单记录 → `premise_invalidation` 机制在单 bot 上从未生效 | 无维度 | T14 |
| D-8 | `profile.py:23/31-66` | 画像按 bot 汇总（无 symbol）→ 多币混算 | 无维度 | T13 |
| D-9 | `paper/store.py:170-171` | `realized_pnl_stats` 主动丢弃 `contract` 列 | 无维度 | T13 |
| D-10 | `profile.py:111-140/99-105` | `by_action`/`best_act` 只按动作、不按币 | 无维度 | T13 |
| D-11 | `persona/runner.py:546-578` | `record_trade` 调用点无 symbol | 无维度 | T13 |
| D-12 | `profile.py:82-87` | 核心计数来自账本、`by_action` 来自文件累加 → 口径混用 | 其他 | T13 |
| D-13 | `exchange_pnl.py:441-495/213-227` | `totals` 单份汇总；`fills[]` 有 contract 但只留 200 条 | 无维度 | T13 |
| D-14 | `exchange_pnl.py:374-378` | 游标全账户（有意）→ 分币只能在投影层 | 其他 | — |
| D-15 | `ledger.py:22/99` vs `tradelog.py:69-76` | `trades.symbol` 列**生产恒 NULL** | 无维度 | T15 |
| D-16 | `ledger.py:32-42/55-62` | `plans` 无 symbol 列（heartbeats 进程级，不适用） | 无维度 | T15 |
| D-17 | `ledger.py:150-160` | `recent_trades`/`recent_plans` 无按币过滤 | 无维度 | T15 |
| D-18 | `loop.py:218/229-233` | `_order_context_for` 取"最新一张"（单币单计划写进 docstring） | 硬编码 | T14 |
| D-19 | `loop.py:224-227`、`orders.py:163-175` | `list_open`/`_mine` 无 symbol 判据 | 无维度 | T14 |
| D-20 | `orders.py:276-289` | 共享订单 JSON **有** symbol（只需读取端按币取） | 其他（已具备） | T14 |
| D-21 | `persona/runner.py:601-623` | `_resolve_order_id` 用 `opens[0]` → **可能关错币的单** | 硬编码 | T14 |
| D-22 | `persona/runner.py:599` | 残留 `"BTC_USDT"` 兜底 | 硬编码 | T3 |
| D-23 | `orders.py:67-90` | `create()` 不校验 symbol 属于宇宙 | 未校验 | T14 |
| D-24 | `loop.py:932-943` | `thinking.json` **无 charts 字段** → 图不可审计 | 无维度 | T10 |
| D-25 | `loop.py:123-129/909-921` | `tool_usage[].args` 有 symbol，但 summary 只按工具名 | 无维度 | T10 |
| D-26 | `loop.py:109/582/1342` | `run_once` 不清 `tool_usage` → plan-loop 跨轮累加，统计失真 | 其他 | T10 |
| D-27 | `loop.py:947-949` + `strategist/schema.py:256-284` | `kline` 审计无 symbol 且只取首个有 kline_read 的 chip | 无维度 | T10 |
| D-28 | `cache_guard.py:19-30/50-70` | `cache_stats`/`cache_prefix` 无 symbol；只 hash `system` | 无维度 | T13 |
| D-29 | `decay.py:70-102/80-84` + `loop.py:716-721` | `perf_metrics` 无 symbol；`pnl_usd` 用权益差（含其他 bot） | 无维度 | T13 |
| D-30 | `monitoring/health.py:29-31` | 进程级健康，symbol 维度**不适用**（显式排除） | 其他 | — |
| D-31 | `scripts/` | `_symbol_audit.py` **不存在**；多数脚本只按 bot/工具名统计 | 无维度 | T16 |
| D-32 | `test_profile_ledger_projection.py:24` 等 | 测试基线固化单币；`fills` 测试表**无 `contract` 列** → 多币改动**假绿** | 硬编码 | T17 |
| D-33 | `agent-memory.md:87/128/141-156/251`、`trade-attribution.md:169`、`strategist-context-integrity.md:27` | 文档把单币口径写成设计 | 其他 | T18 |



