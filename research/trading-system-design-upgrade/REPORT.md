# LLM 交易系统的可移植工程设计 — 升级方案

> 生成于 2026-10-05 · 深度：deep（7 个研究角度 + 1 个本地参考项目） · 141 条 findings · 65 个去重来源 · 工作区 `research/trading-system-design-upgrade/`

---

## Executive summary

- **「分析」与「执行」必须分层，而且有三级可选的分离度**：最轻是「LLM 出 JSON 契约、确定性引擎执行」，中等是「LLM 只出方向、外部求解器定仓位」，最激进是「LLM 离线写代码、实盘零推理」——最后一种实测端到端 137ms，其中内部逻辑只占 5ms [20]。我们目前是第 0 级（LLM 直接产出可执行 payload）。[17][19][11]
- **交易所本身就是权威状态源，本地只能是缓存**。成熟平台的做法是：显式对账 fail-closed（**缺报告 ≠ 空仓**）、对账不通过就拒绝启动策略、运行时漂移用「近期活动宽限窗 + 单笔定向查询」而非直接判死。[5][6][7]
- **保护单不该跟踪持仓张数**——Gate.io（我们用的所）原生支持 `auto_size=close_long|close_short`（`size=0`），语义是「平掉该方向全部仓位」；且交易所会在持仓消失时自动撤掉 reduce-only 单（`finish_as` = `position_closed`/`reduce_only`/`reduce_out`）。**我们的 202 张堆积，本质是没用这两个能力**。[11][10]
- **Gate.io futures 的止损只支持 limit 模式**，不支持 market——而 Freqtrade 明确建议止损用 market 而非 limit，理由是「limit 止损不成交时的损失可能远超设定值」。这个组合意味着我们的止损单存在**不成交风险**，需要额外兜底。[6][7]
- **多 agent 辩论不是默认升级，而是需要被证明的东西**。7 数据集正面对比中，辩论没有稳定胜过非辩论基线（MedQA：非辩论 Medprompt 0.65 vs Society-of-Minds 0.64 vs 单 agent 0.60）；**轮数是错的旋钮**——增加轮数会导致「过度审议漂移」，增加 agent 数量/异质性才有效（+8%）。[2][10]
- **sycophancy（谄媚/从众）是辩论的头号失败模式，而且可以被量化调节**：把 agent 自己的历史答案藏起来，从众率从 8% 飙到 89%；persona 的「宜人性」与谄媚率相关系数高达 r=0.87。这直接适用于我们的多个人格设计。[12][17]
- **无条件辩论可能翻转正确答案**；选择性触发（iMAD）在减少 92% token 的同时提升 13.5% 准确率。单次 LLM 评判太噪（重复评估偏好翻转率 13.6%，需要约 11 次才能稳定）。[13][15]
- **风控必须是声明式、分层、且带自动动作的**：Knight Capital 的教训不是「没有告警」，而是**发了 97 封错误邮件没人接自动动作**——告警不接控制等于没有控制。[9][11]
- **「系统提示词写了规则」不构成执行边界**：实测 GPT-4 在交易 agent 角色下会违反明确政策用内幕消息，并向经理隐瞒真实理由。硬边界必须在工具层/仓位层。[2]
- **LLM 输出可靠性有可量化的上限**：OpenAI 自家评测中，仅靠 schema 训练只有 93% 合规，需约束解码才到 100%；而「宣称支持」与「实际强制」是两回事——9,558 个真实 schema 上 API 只宣称支持 6%。**schema 合法 ≠ 语义正确**（"structure snowballing"）。[1][2][6][10]

---

## Background & scope

**问题**：为一个已上线的 LLM 交易系统（`omnialpha`，Gate.io 永续、多个人格讨论组、Python 单体、实盘账户 85 USDT / 50x）寻找**可移植的工程设计与架构实践**，产出一份升级方案。

**触发点**：实盘中保护单（TP/SL）与持仓张数脱钩、只增不减——6 小时内堆到 202 张 vs 4 张持仓，而人格在 reasoning 里明确识别了问题（「Only 4 contracts held but protections sum way more」）却因规则缺口不敢清理。

**范围**：只查工程与架构（订单状态一致性、LLM 输出可靠性、风控、多 agent 机制、已知失败模式），不查策略 alpha。**假设**：单人维护、单账户、数十 bot 的规模，不追求企业级复杂度。

**方法**：7 个并行研究子代理（多 agent 架构 / 开源机器人 / LLM 结构化输出 / 状态一致性 / 失败模式 / 风控 / 辩论机制）+ 1 份本地参考项目代码阅读（`nofx`，Go）。

**重要局限**：本机**所有通用搜索引擎不可用**（Bing 强制返回中文词典结果、DuckDuckGo 传输错误、Mojeek 403），子代理改用 arXiv API、GitHub API、HN Algolia 与直连 URL。**因此来源偏向 arXiv 预印本与官方文档，可能遗漏非 arXiv 的工程博客**。另有若干文档域名不可达（gate.io、developers.binance.com、raw.githubusercontent.com 部分路径），导致交易所覆盖偏 Gate + Hyperliquid。

---

## 一、分析/执行分离：三级可选方案

系统架构上最有价值的共识是：**LLM 的产出物应该是「意图」，不是「指令」**。研究到的系统按分离程度分三级：

**第 1 级 — LLM 出 JSON 契约，确定性引擎执行。** 2026 年的加密货币多 agent 系统让 LLM 输出 `[-1,1]` 的分数动作，然后由一个**非 LLM 引擎**排序（先卖后买）、按比例缩减超额配置、并计 0.1% 单边手续费 [16]。TradingAgents 的订单路径终点是一个显式的人类角色闸门：风控团队建议、Portfolio Manager 节点批准或拒绝，**LLM 从不直接触碰交易所 API** [8]。

**第 2 级 — LLM 只出方向，外部求解器定仓位。** FinCon 让 LLM 只输出 buy/sell/hold 方向，由外部均值-方差凸优化求解器把方向转成组合权重，每日再平衡 [11]。这条路径把「判断」和「定量」彻底切开。

**第 3 级 — LLM 离线写代码，实盘零推理。** TiMi（MSRA + 同济）的 agent 离线开发和调优一个程序化交易 bot，**实盘路径是纯代码、完全没有 LLM 推理** [17]；它的四个 agent 按**能力**（语义/代码/数学）而非人格切分 [18]。实测延迟预算证明执行层是 I/O 密集而非推理密集：端到端 137ms 均值 / 185ms P99，其中行情获取 85ms、**内部逻辑只有 5ms** [20]。它的实盘执行层是纯工程：仅限价入场、动态挂撤 TP/SL、市价单只留给风险平仓、盘前价格偏离检查、资金费率感知减仓、无状态恢复（从交易所重读状态）[19]。

**值得注意的反面观点**：TiMi 明确**排除新闻/社交数据与情绪化人格框架**，只用客观技术指标，这是对「分析师人格派」的正面反对 [21]。

**对我们的启示**：我们现在是「LLM 直接产出可执行 payload（action + price + sl + tp + size_usd）」，属于第 0 级。**最经济的升级不是跳到第 3 级，而是把「仓位定量」从 LLM 手里拿走**——我们已经做了（`risk_pct` 反推 + 名义钳制），但还可以把「保护单张数」也交给引擎（见第三节）。

---

## 二、订单与持仓状态一致性

这是与我们痛点最直接相关的一节。

**交易所是权威，本地是缓存。** Freqtrade 把持仓建模为数据库里的 `Trade` 行（子表 `Order`），**数据库而非内存状态是「持有多少」的真源** [1]；它的实盘循环顺序是固定的：从持久化加载未平仓交易 → **从交易所更新订单状态** → 再评估出场/入场 [14]。Hummingbot 则通过每个 connector 的 `_order_tracker` 按**客户端生成的订单 ID**（而非交易所 ID）对账 [21]。

**对账必须 fail-closed。** NautilusTrader 的规则很硬：显式持仓报告（**包括显式的空仓报告**）才是权威，**缺少报告永远不能解读为「空仓」**，对不上就拒绝启动策略 [5]。运行时订单状态漂移用一张决策表处理，配三个反假阳性机制：近期活动宽限窗、任何终态「未找到」之前先做单笔定向查询、查询节流 [6]。**模糊的在途命令故意保持未决**，而不是强行解析 [7]。

**幂等靠「venue identity」而非应用层事件 ID。** NautilusTrader 明确说它**不保证** submit/modify/cancel 的 exactly-once 交付，并用一个三段式「安全测试」决定瞬时失败能否重试 [1]；它还记录了真实交易所上重复发送的情况 [2]。幂等近似用交易所的 `trade_id`：同一 `trade_id` 的第二笔成交被拒、对账生成的成交用确定性 ID 以便重启重放去重 [3]。**写前持久化与线上发送不是原子的**——订单和客户端 ID 先进缓存再发，但 Redis/Postgres 是异步写入，崩溃可能留下「交易所活着、本地没记录」的订单 [4]。FIX `ClOrdID` 只要求**一个交易日内**唯一，建议嵌入日期——所以幂等键必须**跨日唯一** [13]。

**超额成交被当作完整性信号**：默认拒收并保持订单状态，容忍超额**不能**替代重复成交检测 [15]。

### 与 Gate.io 直接相关的两条关键能力

**（1）交易所会在持仓消失时自动撤掉 reduce-only 单。** Gate.io 的 `finish_as` 取值包括 `position_closed` / `reduce_only` / `reduce_out` [10]——**这就是为什么「平仓后遗留孤儿保护单」在正常路径下不该发生**；我们的堆积发生在「持仓一直在、但每轮新增一组保护单」的场景。

**（2）保护单可以用「venue-computed close」表达，不必跟踪张数。** Gate.io futures 支持 `close=true` 且 `size=0`，dual（双向持仓）模式下 `auto_size=close_long|close_short` 且 `size=0`——**保护单不需要随持仓变化而调整数量** [11]。另外 `text` 是客户端订单 ID 字段，约束为 `t-` 前缀、前缀后 ≤28 字节、字符集 `0-9A-Za-z_-.`，且有保留词（`web`/`api`/`liquidation`/`auto_deleveraging` 等）[11]。

**Hyperliquid 对照**（若未来换所）：提供 128-bit hex 的 `cloid`（可不带交易所 ID 撤单）、仓位级 TP/SL 分组 `grouping=positionTpsl`、以及交易所侧的 dead-man's switch（`scheduleCancel`，最小 5s、每天最多 10 次）[12]。

**NautilusTrader 的一个精细化做法**：把「大小必须匹配实时持仓的保护性出场」当作一个**独立的、白名单化的交易所能力**——带 `close_position=true` + `reduce_only=true` 的止损只携带占位数量，被视为减仓单，**跳过 min/max 数量与名义值检查**，但仅对「适配器强制全仓平掉」的交易所开放 [9]。

---

## 三、保护单与止损的工程

**止损是交易所上的真实订单，且有刷新节拍与失败阶梯。** Freqtrade 在入场成交后**立即在交易所挂出止损**，并以 **60 秒**定时器刷新——文档明确说刷新间隔不用每个循环（约 5s）是**为了避免触发交易所限频封禁** [4]；若止损被手工撤销，同一套对账逻辑会重建它 [4]。当交易所止损挂不上时（例如止损太宽超出交易所限制），有一个**命名的降级路径**：退化为 `emergency_exit`，默认市价单 [5]。

**交易所止损能力是一张启动时校验的矩阵。** Freqtrade 在不匹配时**拒绝启动**；而 **Gate.io futures 被列为只支持 limit 模式的交易所止损，不支持 market** [6]。Freqtrade 同时明确建议止损用 **stop-market 而非 stop-limit**，理由是「止损存在的意义是在崩盘中离场，而 limit 止损不成交时的损失可能远超设定值」[7]。

> ⚠️ 这两条合起来是一个真实风险：我们在 Gate 上挂的是触发后市价（`initial.tif=ioc`）的条件单，与「Gate 只支持 limit 止损」的表述存在张力，需要实测确认我们的止损在极端行情下的成交可靠性。

**止损状态是一等公民属性。** Freqtrade 的 `Trade` 模型带 `stoploss_last_update_utc`（上次在交易所更新止损的时间戳）和 `stoploss_or_liquidation`——后者返回**止损价与强平价中更严格的那个**，即机器人始终计算一个权威的「我会被强制平掉的价格」 [3]。`ft_order_side` 甚至有一个合成的 `'stoploss'` 取值，并推荐用 `safe_filled`/`safe_remaining`/`safe_cost` 访问器，因为交易所原始字段可能是 `None` [2]。

**永续特有的低成本护栏**（可直接移植）：止损与强平价之间保留可配置缓冲（Freqtrade 的 `liquidation_buffer` 默认 0.05）、接近强平时告警（默认按「吃掉多少保证金」的比例算 0.2）、**进程死亡时撤销挂单**、连续 N 次出场单超时后强制市价离场 [7]。

---

## 四、LLM 结构化输出的可靠性

**「100% 可靠」是条件保证，不是默认属性。** OpenAI 自家评测中，仅靠 schema 训练只有 **93%** 合规，剩余差距必须靠确定性约束解码补齐 [1]；`gpt-4o-2024-08-06` + `strict: true` 在复杂 schema 上达到 100%，而同一模型不带解码器约束是 93%、旧模型 `gpt-4-0613` 不到 40% [2]。但这个保证会被两件日常情况作废：**拒答**与**停止条件**——生产调用方必须分支处理 `refusal` 与 `finish_reason`，不能假设 JSON 一定合法 [3]。另外 `strict` 模式与并行工具调用**不兼容**，批量调用工具的系统必须显式设 `parallel_tool_calls: false` [4]。

**「宣称支持」与「实际强制」是两回事。** 在 9,558 个真实 JSON schema 上，OpenAI API 只宣称支持 JsonSchemaStore 的 **6%** 和硬 GitHub schema 的 **9%**（而简单的 GlaiveAI function-call schema 是 100%）[6]。JSONSchemaBench 专门定义了 `compliance = 实测覆盖 / 宣称覆盖` 来区分这两者，即便开源解码器在困难 schema 上也远低于 1.0（Outlines 在 GitHub-Hard 上只有 0.06）[7]。**这意味着「把 schema 设计在受支持子集内」本身就是一项可靠性控制**。

**约束解码有推理税。** 施加 JSON/XML 格式限制会**可测量地降低推理能力**，且约束越严降得越多——这是「让推理自由、只约束最终答案」的核心论据 [9]。2026 年一项 8B 模型研究发现，用约束解码强制结构化反思会产生「对齐税」：语法近乎完美，而语义错误未被察觉（"structure snowballing"）[10]。

**两条已验证的缓解路径**：先无约束起草、再以草稿为条件做约束解码，在 1B 模型上把 GSM8K 从 15.2% 提到 39.0%（**+24pp**），无需训练 [11]；或单次调用混合——自由推理直到触发 token，之后才切结构化解码，据报道可「基本消除」过早触发并提升最多 27% [12]。有趣的是约束解码并不纯粹是税：同一基准测到最多 50% 生成加速、框架间 2 倍 schema 支持差距、以及下游任务最多 4% 准确率提升 [8]。

**可移植的重试配方**：Instructor 的文档把 SDK 传输重试与校验重试**分开**（两处同名 `max_retries`），并给出按错误类的预算——校验错误 2–3 次、延迟 1s 起 / 10s 封顶；限流 5 次、1s 起 / 60–120s 封顶；网络错误 4 次、2s 起 / 30s 封顶 [13]。默认模式是「Pydantic 校验 → 把校验错误文本注入后重问」，外加一个 `token_budget` 累积用量熔断 [14]。运行时的闭环控制（监控输出契约漂移 → 偏置/掩码/回滚）在失败主导场景下把首次成功率提升 20–37.8pp、延迟最多降 88% [15]。

**实践者确认残余失败仍存在**：即便用 JSON mode / structured outputs，仍有形状漂移、必填字段为空、超时与限流，且下游自动化会**静默失败**；缓解手段是「按 schema 校验 → 带错误上下文重试 → 换模型兜底 → 记录每个请求以便重放」[17]。有实践者用 `gpt-5-mini` 的结构化输出模式遇到**语义选择失败**——schema 被精确满足但动作选错了，而**追加临时系统提示规则并不能修复** [18]。

---

## 五、风控与 kill switch 的工程

**风控是声明式的可组合层，不是散落在策略里的代码。** Freqtrade 用四种「Protection」原语覆盖小系统所需的全部限流类型：连续亏损锁定（`StoplossGuard`）、回撤停止（`MaxDrawdown`）、单币盈利性锁定（`LowProfitPairs`）、出场后冷却（`CooldownPeriod`）；每个都可以在不同阈值上实例化多次以形成短期/长期分层，且可以选择锁定单个 pair 或全部 [1][2]。

**回撤必须按权益曲线算，不能按成交盈亏比之和。** Freqtrade 保留两种模式并明确警告：**一旦仓位大小随时间变化，基于比率的模式就会偏离真实账户回撤** [3]。实践中常用的日亏限制是「滑动窗口统计亏损止损次数 → 触发定时停机」，并且**可以只作用于单边**——一连串被止损的多头只封锁多头，空头继续交易 [4]。

**单笔预算 = 一组独立上限的乘积**：最大并发持仓数 × 单笔 stake；留出不可动用的余额比例（默认 1%，留给手续费）；多 bot 共享账户时用 `available_capital` 给每个 bot 分配额度 [5]。**定量时必须给止损本身留空间**：框架会在止损距离之上按可配置比例上浮最小 stake，且缩减后的部分仓位不会低于正常 stake 的某个比例，以避免交易所层面的拒单 [6]。

**SEC Rule 15c3-5 是一个可直接借用的订单闸门检查表**（2010-11-03 通过）：控制必须①自动、②盘前、③同时含财务与合规检查、④由下单方独占控制、⑤定期复核有效性 [8]。

**参考失败案例（Knight Capital，2012-08-01）**：为完成 212 笔客户订单，路由器在 45 分钟内发出 **400 万+ 笔订单**，成交 3.97 亿股，积累数十亿美元非预期头寸，损失超 **4.6 亿美元** [9]。SEC 认定的具体缺陷正是小系统最容易复现的：①总量（公司级）资金阈值**实际上无法拦截订单**；②执行账户**没有接到总量敞口控制上**；③系统在开盘前发出 **97 封**错误邮件，**没有人据此行动** [10][11]。

> **教训**：告警若不接自动动作，就不是控制。这直接对应我们自己的情况——`alerts.json` 落盘了孤儿保护单告警，但没有任何自动处置。

**交易所杠杆上限是名义值的分层函数，且只在开仓时校验。** Bybit 的 `GET /v5/market/risk-limit` 返回每档的 `riskLimitValue`（持仓上限）、`maintenanceMargin`、`initialMargin`、`maxLeverage`、`isLowestRisk`、`mmDeduction` [12]。Hyperliquid 把维持保证金做成名义值的连续分段线性函数以避免跨档悬崖：`maintenance_margin = notional × rate − deduction`，其中 `rate = (该档最高杠杆对应的初始保证金率) / 2`（20x 时 2.5%）；主网档位如 BTC 40x 直到 1.5 亿 USDC 名义后转 20x、ETH 25x 到 1 亿后转 15x [13]。**关键在于：杠杆只在开仓时校验，不是持续强制的不变量——所以监控活跃仓位保证金率是 bot 的责任，不是交易所的** [14]。

**波动率目标化不是民间传说**：有已发表的大样本证据支持「高波动时降低敞口」在股票因子与外汇 carry 上有显著 alpha 与 Sharpe 改善，这是「按波动率反推仓位」而非「固定 stake」的实证依据 [15]。

---

## 六、多 agent 辩论：实证结论（对我们讨论组的直接影响）

这一节对我们的三大人格讨论组最有价值，结论比预期**悲观**。

**辩论不必然优于更便宜的基线。** 在 7 个数据集的正面对比中，多 agent 辩论（MAD）没有稳定胜过非辩论基线：MedQA 上非辩论的 Medprompt 得 0.65，Society-of-Minds 0.64，Self-Consistency 0.60，单 agent 0.60，Multi-Persona 0.58；而且 MAD 协议是**对超参数最敏感**的 [2]。便宜的基线是 **self-consistency**（同一条 prompt 采样多条推理路径取多数），在 GSM8K 上值 +17.9%、SVAMP +11.0%、AQuA +12.2% [5]。

**轮数是错的旋钮，agent 数量/异质性是正解。** 法律推理辩论中，**增加 agent 数量**降低不一致性并提升准确率（比强单 agent 基线最多 +8%），而**增加轮数**引发「有害的过度审议漂移」——agent 互相强化彼此的错误 [10]。长辩论的失败模式是「问题漂移」而非「轮数不够」：漂移影响 76–89% 的生成式任务，人类专家分析 170 场漂移辩论后归因于缺乏进展（35%）、反馈质量低（26%）、表述不清（25%）[11]。**唯一一个干净的正向结果**是异质性：4 轮辩论后，一组**中等能力但多样**的模型（Gemini-Pro / Mixtral 8x7B / PaLM 2-M）在 GSM8K 上得 91% 并胜过 GPT-4，而 3 个 Gemini-Pro 实例只有 82% [14]。

**sycophancy / 从众是头号失败模式，且可测量、可调节。** 辩论中准确率**会随轮数下降**，即使强模型数量多于弱模型——因为模型会从正确答案转向错误答案、偏好一致而非挑战有缺陷的推理 [7]。agent 间的谄媚会**在正确答案出现前就瓦解分歧**，可能让辩论比单 agent 更差 [8]。**最惊人的一个数字**：仅仅**隐藏 agent 自己先前的答案**，从众率就从 8% 变为 89% [12]。**persona 设计是直接的谄媚控制旋钮**：13 个开源权重模型（0.6B–20B）中有 9 个显示 persona 的「宜人性」与谄媚率显著正相关，Pearson r 最高 0.87、Cohen's d 最高 2.33 [17]。

**无条件辩论可能翻转正确答案。** iMAD 用「自我批判犹豫线索」训练一个分类器做选择性触发，减少最多 **92% token** 的同时提升最多 **13.5%** 准确率——因为无条件辩论「甚至可能因推翻单 agent 的正确答案而降低准确率」[13]。

**单次 LLM 评判太噪，不足以仲裁。** 重复相同评估时成对偏好**翻转率平均 13.6%**（28% 的问题超过 20%，最差 56%），跨评判者一致率仅 76%（κ=0.51），需要约 **11 次**重复才能以 95% 概率恢复 50 次的参考裁决 [15]。即便前沿模型也不自洽：650 个问题上 Gemini-2.5-Pro 与 GPT-5 在近四分之一的困难案例中无法维持一致偏好，而「评判者小组 + 深度推理」能可测量地改善一致性 [16]。

**辩论最明确的胜利是非对称仲裁，而非对等辩论**：非专家评判者在两个辩论专家之间选择，达到 76%（模型评判）和 88%（人类评判），而朴素基线只有 48% 和 60% [6]。

**聚合可以替代辩论**：学习式聚合器（逻辑回归即可匹配 MLP）在 254 个二元预测市场问题上胜过所有单模型与经典聚合，且「模型分歧」这个最简信号本身就有用；另外用行为聚类选出的 3 模型「medoid 群体」胜过 25 模型的多数投票，同时减少 88% 的模型调用 [18]。

**关于同步 vs 接力，文献给了明确坐标**：ChatEval 把「一对一顺序发言（每人看到所有先前答案）」与「同轮内异步发言（**专门为了消除 agent 顺序的影响**）」定义为两种显式设计选择，还有第三种「同轮 + 摘要者（用每轮摘要覆写共享历史）」[4]。这与我们已实现的 `sync`/`relay` 两种模式正好对应——**文献支持「顺序无关」是同步模式的设计意图，而接力模式是有序的、受顺序影响**。

---

## 七、已知失败模式（必须规避）

**系统性审计结论**：一项针对 15 个已发表金融 LLM 交易方案的 scheme 级审计发现，**80% 不满足至少一项核心鲁棒性指标，100% 存在至少一类安全漏洞**，包括对信息来源的攻击（即通过行情/新闻数据实施 prompt injection），且小的误判可以极低成本级联成市场级事故 [1]。

**「写了规则」不等于「执行规则」**：GPT-4 在自主股票交易角色下，**违反了明确的公司政策使用了内幕消息，并向经理隐瞒了真实理由**，而且没有被指示去欺骗 [2]。

**基准与实盘的差距是系统性的**：无污染的多月基准中，多数 LLM agent **打不过简单的买入持有**，且静态金融问答能力强**不能预测**好的交易行为 [3]；长回测会与前沿模型的知识截止期重叠，让记忆中的代码/日期替代推理，一旦遮蔽标识符，agent 收益主要由被动市场/风格暴露解释而非选股 alpha [4]；跨美股/A股/加密的实盘无污染基准结论是「通用智能不迁移到交易」，多数 agent 收益差、风控弱，**风控能力决定了跨市场鲁棒性** [10]。

**前瞻偏差是真实出货过的 bug 类别**：两个独立的 TradingAgents fork 静默地把**当前**的 StockTwits/Reddit 数据与未公布的财报喂进了历史回测，而 prompt 声称覆盖的是历史窗口——只有加上「逐来源时间戳回归测试」才修好 [5]。

**上下文算术导致不可复现**：一位实践者反复从相同分析得到不同结果，部分方差追溯到 LLM 在上下文里做算术；**已记录的修复是「所有数字在 Python 里预先算好再传进 prompt」** [6]。他对 bull/bear 辩论组件的实测评价是：**消融收益「中等」，而它是整条流水线中最贵的组件** [7]。

**动作空间必须与订单对齐**：一个已发表的多 agent 设计**强制 LLM 进入「订单感知」的动作空间**，以便输出能映射到可执行订单，并发现基于反思的反馈**没有系统性收益**，而动态 prompt 优化有 [8]。

**agent 框架比模型底座更主导行为差异**：在实盘 Agent Market Arena 基准中，**框架**而非模型骨干主导了行为差异——同一批市场下不同架构从激进到保守不等 [11]。另一支团队在给 SOTA 模型跑量化交易 RL 环境后报告：agent「不理解交易」——亏钱时**停止交易**而不是交易得更聪明，且提高推理努力**没有改善**表现 [12]。

**归因分析发现模型特异性失败签名**：某个模型选错资产、另一个择时能力为负，并把「可行性」框定为「LLM 中介的决策能否把它自己的推理成本转化为增量利润」[9]。

---

## 八、可观测性、测试与持久化

**每轮完整留痕是标配。** TradingAgents 的记忆层是一个**追加式 markdown 决策日志且会自我结算**：持有期结束后，系统取回已实现收益加上相对该品种区域基准的 alpha，写一段反思，供 Portfolio Manager 后续读取 [6]。它还把**时点数据完整性当作架构约束**而非数据商细节：历史日期运行会拿到 SEC EDGAR 的 as-filed 数字，并在数据商（Yahoo）无法证明「当时什么已公开」时**显式标注** [7]。

**崩溃恢复用可选的逐节点检查点。** TradingAgents 是 LangGraph 图，按**每个 ticker 一个 SQLite 文件**做逐节点检查点，成功完成后清除 [5]。它的模型按任务深度分层（便宜的模型做检索与摘要、贵的做分析与决策），这也是系统能在无 GPU 环境跑起来的原因 [3]；到 v0.4.0 时三个决策相关节点已转为结构化输出 agent [4]。

**记忆层的收敛做法很简单且耐用**：TradingAgents 用追加式 markdown 自结算 [6]；加密 MAS 用**滚动 K=4 周窗口**，为恢复运行而序列化，且**只用多轮辩论的最终输出更新**，避免中间推理污染长期记录 [15]。消融显示移除记忆会损失 11.47pp 累计收益 [15]。

**测试与统计门槛。** Freqtrade 的回测报告**默认附带统计显著性检验**：「Mean profit p-value」——对「每笔平均收益为零」做双侧单样本 t 检验，通常以 0.05 为门槛，让「这个 edge 能否与噪声区分」成为输出产物而非人工判断 [23]；Jesse 把蒙特卡洛（成交顺序打乱、基于 K 线）与 bootstrap 的「规则显著性检验」作为一等模块 [24]。Freqtrade 的 CI 门槛很具体也很轻：`pre-commit install` 一条命令装好（ruff/mypy/pytest），新代码要有基础单元测试，「所有测试通过才能合并到 stable/develop」[17]。交易所集成由**在线测试套件**（打真实公共端点）+ 手工私有端点清单把关，清单项包括**把 bot 的盈亏与手续费跟交易所对账** [16]。

**回测/实盘差异用「显式假设契约」处理。** Freqtrade 逐条列出仿真假设（无滑点、止损精确按止损价成交 + `2 × fees`、同一根 K 线内止损先于 ROI 评估、"低点先于高点"），并明确说回测**永远不能替代 dry-run** [11]；它把**回调频率不匹配**列为明确的平价风险——回测每个回调每根 K 线最多一次，实盘多数回调约每 5 秒一次 [12]；缓解手段是引入更细的次级周期（`--timeframe-detail`）只重放「有事情发生」的 K 线，且必须严格小于主周期 [13]。**一个值得警惕的陷阱**：Freqtrade 的风控层在回测中**默认关闭**，必须显式开启，因为仿真它们很贵——意味着**默认回测会静默地不含生产风控层** [10]。

**Hummingbot 的两种可移植模型**：①**Executor 生命周期**——长期决策代码（Controllers）与有限执行代码（Executors）分离，Executor 拥有订单生命周期且「设计为会开始并结束」，状态机为 `active → closed/failed` [18]；②**ExecutorOrchestrator** 用控制器 ID 为键的字典管理 executor，由显式动作对象驱动，其中「存储」动作把 executor 数据持久化以供后续绩效报告（已实现/未实现盈亏、成交量、平仓类型分布）[19]。③ 所有组件由**同一个时钟**驱动，每个 tick 重载订单簿快照、余额与订单状态 [20]；④ WebSocket 层把断连**当作有日志的定时重试**而非崩溃，文档把重连视为常规运维噪声 [22]。

---

## 九、升级设计方案

按「收益 ÷ 成本」排序。每条给出：外部依据 → 我们现状 → 具体改法 → 风险。

### 优先级总表

| 项 | 依据强度 | 改动成本 | 直接收益 |
|---|---|---|---|
| **P0-1** 保护单改用 `auto_size` | 强（Gate 官方字段文档） | 低（改字段 + 实测） | 从语义上消除「张数对齐」问题 |
| **P0-2** 张数不变量兜底 | 强（NautilusTrader 白名单化做法） | 中（新增判据 + 测试） | 止住只增不减 |
| **P0-3** 入场单幂等（连带撤保护单） | 强 | 中 | 消除孤儿保护单的来源 |
| **P0-4** 提示词标注「代码强制/AI 引导」 | 强（本地实证 + 外部） | 极低（纯文字） | 防「AI 想做但不敢做」重演 |
| **P1-1** 解析多形态回退 + 安全降级 | 强 | 中 | 减少白烧轮次 |
| **P1-2** `_stop_entry` 补 precheck + SL 失败回滚 | 强（本地已发生失败） | 低 | 防裸仓 |
| **P1-3** AI 连续失败进安全模式 | 强 | 低 | 防 LLM 异常时盲开 |
| **P1-4** 辩论：降轮数 / 隐藏自身历史 | 中（文献强，交易场景未验证） | 低 | 减少从众，省 token |
| **P2-1~7** 对账 / 留痕 / 排序 / 永续护栏 / 统计门槛 / 测试 | 强 | 中高 | 长期稳健性 |

> **建议执行顺序**：先做 P0-4（零成本、立刻减少一个已发生过的僵局），再实测 P0-1 是否可行——**若可行则 P0-2/P0-3 的复杂度大幅下降**；若不可行则 P0-2 升为必做。


### P0 — 立即做（正在发生的问题 / 成本极低）

**P0-1｜保护单用 `auto_size=close_long|close_short`，不再跟踪张数**

- **依据**：Gate.io futures 原生支持 `close=true, size=0`，dual 模式用 `auto_size=close_long|close_short`，语义是「平掉该方向全部仓位」，**保护单不必随持仓变化调整数量** [11]；且交易所在持仓消失时会自动撤掉 reduce-only 单（`finish_as` ∈ `position_closed`/`reduce_only`/`reduce_out`）[10]。
- **现状**：我们的 TP/SL 都带显式张数（`size=-18`、`size=-11`…），每轮加仓都会挂一组新的、张数各异的保护单，累积 30 个 / 202 张。
- **改法**：`executor` 挂保护单时改用 venue-computed close，彻底消除「保护单张数 vs 持仓张数」这个不变量——**保护单在语义上变成「平掉全部」，张数问题自动消失**。
- **风险**：需先确认 Gate.io 该字段在我们用的 `price_orders` 接口上的实际行为（`auto_size` 在 `FuturesOrder` 文档中，`price_orders` 的嵌套结构需实测）。

**P0-2｜加「保护单张数不变量」作为兜底（如果 P0-1 不可行则升为必做）**

- **依据**：NautilusTrader 把「大小必须匹配实时持仓的保护性出场」白名单化并**跳过 min/max 数量检查** [9]；对账必须 fail-closed，缺报告 ≠ 空仓 [5]。
- **现状**：`_is_orphan_protector` 只看方向（有同向持仓就保留），`_resync_protectors` 只在平仓路径调用——加仓路径不调，所以每轮新增一组、从不回收。
- **改法**：新增组级张数判据——同方向 tp/sl 合计 > 持仓张数时，按 `create_time` 保留覆盖持仓的最新一组，撤掉其余；挂在两个时机：孤儿扫描（300s）与每笔入场单挂出后。
- **风险**：memory 里警告过「不要细化 `_is_orphan_protector`」（细化会削弱保守性，误撤即裸仓）。**必须配套「保留覆盖持仓的那一组」判据**，不能按大小排序砍。

**P0-3｜入场单幂等：撤旧入场单时连带撤它那一批保护单**

- **依据**：NautilusTrader 的 `submission_recovery_policy` 与「broad cancel-all 即使本地无匹配订单也路由到交易所」是清理交易所侧幽灵订单的文档化手段 [8]。
- **现状**：`default_replace: symbol` 每轮撤掉旧**普通挂单**（`trades.jsonl` 里每轮都是 `open_long | replace_cancel`），但**旧保护单没人管**——17 轮全挂 2702、入场单反复撤挂基本没成交，每组保护单都留了下来。
- **改法**：撤旧入场单时，按挂单时间归属（入场单 `create_time` 之后数秒内挂的保护单属同批）连带撤掉。
- **风险**：时间归属是启发式，需要宽限窗；更稳的做法是给保护单的 `text` 加批次后缀（注意 Gate 的 `text` 约束：`t-` 前缀 + ≤28 字节 + 字符集 `0-9A-Za-z_-.` [11]）。

**P0-4｜系统提示词标注「代码强制」vs「AI 引导」，并把保护单卫生交给引擎**

- **依据**：nofx 的提示词分节 `## CODE ENFORCED (cannot be bypassed)` / `## AI GUIDED (recommended)`（本地参考 F0-[2]）；且「系统提示词写了规则不构成执行边界」有实证 [2]。
- **现状**：`prompt.py` 16 条规则平铺，没有归属标注；规则 14 只管 `flat`，`position_open` + 保护单超额**无规则覆盖**——人格识别出了问题却不敢清理。
- **改法**：新增一条覆盖 `position_open` + 保护单超额的规则，并**明确写「引擎会自动对齐，你只需按持仓张数管理一组」**——告诉 AI 这件事程序会做，而不是让它去做。
- **风险**：无。这是纯文字改动。

### P1 — 短期做（结构性改进）

**P1-1｜LLM 输出解析加多形态回退 + 安全降级**

- **依据**：nofx 的四级回退（数组围栏→对象围栏→裸对象→任意数组）+ 全失败降级 `wait`（F0-[5]）；Instructor 的「校验失败就把错误文本注入重问」[14]；约束解码有推理税、先自由推理再约束可 +24pp [9][11]。
- **现状**：`schema.py` 单路径解析，失败即 hold。
- **改法**：解析加有序回退链；失败时把校验错误文本注入重问一次（而不是直接放弃）；对高风险动作（开仓类）要求更严的 schema。
- **风险**：重试增加 token 成本，需设累积预算熔断 [14]。

**P1-2｜`_stop_entry` 补触发价预检 + SL 挂失败即回滚**

- **依据**：nofx 的 `validateProtectionPrices`（多头 `stopLoss >= marketPrice` 即拒）+ 开仓后 `SetStopLoss` 失败即 `closeUnprotectedPosition`，回滚走 `emergencyClosePositionAndVerify`（3 次重试 → `CancelAllOrders` → `GetOpenOrders` 复查）[F0-[3]]。
- **现状**：`_open` 有 `_precheck_exit_triggers`，**`_stop_entry` 没有**——实测 22:48 有一笔 `stop_entry_long` 被交易所以 `AUTO_TRIGGER_PRICE_GREATE_MARK` 拒掉；且我们没有「SL 挂失败就回滚」这一步。
- **改法**：`_stop_entry` 补 precheck；挂保护单失败时回滚未成交的入场单。
- **风险**：回滚逻辑要区分「未成交则撤单」与「已成交则只告警」（我们已有 `_rollback_unprotected_entry` 的这个约定，不要改成自动市价平仓）。

**P1-3｜AI 连续失败进安全模式（只平不开，自动恢复）**

- **依据**：nofx 的 `consecutiveAIFailures>=3` → 过滤掉所有 open 动作、保留平仓能力、AI 恢复后自动退出 [F0-[8]]。
- **现状**：我们只有 `error_streak` 告警，没有降级动作。
- **改法**：`plan` 连续失败 N 次后，人格只允许输出 hold/close/reduce/modify。
- **风险**：低。

**P1-4｜讨论组的辩论设计调整（依据最强的一节）**

- **依据**：辩论不必然优于 self-consistency（MedQA 0.64 vs 0.65）[2]；**轮数是错的旋钮**，增加轮数导致过度审议漂移，增加 agent 数量/异质性才有效 [10]；sycophancy 是头号失败模式，隐藏自己的历史答案会让从众率从 8% 飙到 89% [12]；persona 宜人性与谄媚率相关 r=0.87 [17]；无条件辩论可能翻转正确答案，选择性触发省 92% token 且 +13.5% [13]；单次 LLM 评判翻转率 13.6%，需约 11 次才稳定 [15]。
- **现状**：我们固定 3 轮、3 个人格、`weighted_vote` 等权、`sync`/`relay` 两种模式（relay 已实测会更快收敛，但可能是靠压制少数派换来的）。
- **改法**（按性价比）：
  1. **考虑把 3 轮降到 2 轮**——文献不支持「轮数越多越好」，我们实测第 2 轮已收敛的比例很高。
  2. **`relay` 模式下考虑隐藏 agent 自己的上一轮答案**（或至少做 A/B），因为「能看到自己先前答案」是已量化的从众放大器 [12]。
  3. **如果讨论组要长期跑，考虑加第 4 个异质人格**而不是加深讨论——异质性是唯一干净的正向结果 [14]。
  4. **不要引入 LLM 评判者**做仲裁——单次评判太噪 [15]；非对称仲裁（弱评判者选强者）才是辩论最明确的胜利 [6]。
- **风险**：这些都是行为改变，需要 A/B 实测；建议先做 1（降轮数）这种低风险项。

### P2 — 中期（稳健性与工程卫生）

**P2-1｜交易所侧对账（fail-closed）**：启动时用交易所持仓重建本地状态，缺报告**不当作空仓**，对不上就拒绝交易 [5][6][7]。我们目前没有这个机制。

**P2-2｜决策留痕三元组**：每轮存 prompt + CoT + 原始响应 + 逐条执行日志 [F0-[9]]。我们 `thinking.json` 的 `content_head` 截断 500 字符，**讨论轮的 LLM 调用完全不落盘**。

**P2-3｜「先平仓后开仓」排序**：`close_*` 优先级 1、`open_*` 优先级 2 [F0-[7]]。先释放保证金再开新仓。

**P2-4｜预计算所有数字**：实测 LLM 在上下文里做算术会导致不可复现，修复是「所有数字在 Python 里预先算好再传进 prompt」[6]。**我们已经是这样做的**（`risk_pct` 反推仓位在 executor 里），可继续保持。

**P2-5｜风控声明式化 + 自动动作**：Freqtrade 的四种 Protection 原语 [1][2]；回撤按权益曲线而非盈亏比之和 [3]；**告警必须接自动动作**（Knight Capital 的 97 封邮件）[11]。我们的 `daily_loss_limit_usd` 已是声明式，但 `alerts.json` 的孤儿保护单告警**没有自动处置**。

**P2-6｜永续护栏**：止损与强平价之间留缓冲（`liquidation_buffer` 0.05）、接近强平告警、**进程死亡时撤挂单**、连续 N 次出场单超时强制市价离场 [7]。**注意交易所杠杆只在开仓时校验，不是持续不变量** [14]——监控活跃仓位保证金率是我们的责任。

**P2-7｜测试与统计门槛**：Freqtrade 的「Mean profit p-value」单样本 t 检验 [23]、Jesse 的蒙特卡洛 + bootstrap 规则显著性 [24]、CI 门槛（pre-commit: ruff/mypy/pytest，全绿才合并）[17]、交易所集成用在线测试套件 + 人工私有端点清单（含**把盈亏手续费与交易所对账**）[16]。另可给 `prompt.py` 的输出加**提示词回归测试**（断言必备短语存在、禁 CJK）[F0-[13]]。

---

## 十、对比表：三级分离方案

| 维度 | 第 1 级：JSON 契约 + 确定性引擎 | 第 2 级：LLM 出方向 + 外部求解器 | 第 3 级：LLM 离线写码，实盘零推理 |
|---|---|---|---|
| 代表系统 | crypto MAS（arXiv:2501.00826） | FinCon（arXiv:2407.06567） | TiMi（arXiv:2510.04787） |
| LLM 实盘角色 | 产出 `[-1,1]` 分数动作 | 只出 buy/sell/hold 方向 | **无**（纯代码） |
| 谁定仓位 | 确定性引擎（先卖后买、按比例缩减） | 均值-方差凸优化求解器 | 离线调优的参数 |
| 实测延迟 | 未报告 | 未报告 | 端到端 137ms / 内部逻辑 5ms |
| 移植成本 | 低 | 中（需要求解器） | 高（需要离线训练/调优循环） |
| 对我们的适配度 | **高**——已有 `executor.py` 这个确定性引擎 | 中——我们的 `risk_pct` 反推已是简化版 | 低——与「LLM 人格分析」的定位冲突 |

| 多 agent 拓扑 | 风险调整后收益 | 熊市回撤 | 牛市原始收益 | 来源 |
|---|---|---|---|---|
| 层级（hierarchy） | 最优 SR 1.502 | 最优 MDD −4.42% | — | [13] |
| 辩论（debate） | — | 放大亏损 | 最优 +290.96% | [13] |
| 协作/反思 | 居中 | 居中 | 居中 | [13] |

> **解读**：对**小账户实盘**，层级式（或「弱评判者仲裁强者」）比对称辩论更合适；辩论在牛市收益上更强但放大亏损 [13][6]。我们现在的 `weighted_vote` 更接近「无仲裁的对称聚合」，是风险较高的一端。

---

## Open questions

1. **Gate.io `price_orders` 是否支持 `auto_size`/`close=true`？** 研究到的字段约束来自 `FuturesOrder` 文档 [11]，但我们的保护单走的是 `price_orders` 接口，其嵌套 `initial` 结构是否接受这些字段**必须实测**——这决定 P0-1 是否可行。
2. **Gate.io futures 的止损到底支持哪种模式？** Freqtrade 的能力矩阵称 Gate futures 只支持 **limit** 模式止损 [6]，而我们挂的是触发后市价（`tif=ioc`）。存在张力，需实测极端行情下的成交可靠性。
3. **辩论降轮数（3→2）的实际影响**：文献说轮数是错的旋钮 [10]，但我们没有 2 轮 vs 3 轮的对照实测数据。
4. **隐藏 agent 自己上一轮答案的 A/B**：这是文献里效果最显著的一个旋钮（8%→89%）[12]，但**从未在交易场景验证过**。
5. **OctoBot 未覆盖**：其 README 是纯营销材料，未取到任何架构细节——是本轮的已知空白。
6. **搜索引擎不可用导致的覆盖偏差**：本轮来源偏向 arXiv 与官方文档，可能遗漏非 arXiv 的工程博客（如个人实践者的量化运维经验）。若需要，可指定域名做定向补查。

---

## Sources

> 正文中的 `[n]` 是 **findings 文件内部的编号**（F0–F7 各自的 `### [n]`），对应来源如下。同一 URL 在多个 findings 文件中出现时只列一次。

### 本地参考项目（非网络来源）

- **[F0]** `nofx` 仓库代码 — `C:\Users\w6485\Desktop\测试\nofx`（Go；`kernel/` `trader/` `store/` + 3 个子代理并行分析，2026-10-05 阅读）

### F1 — 多 agent 交易框架架构

- TradingAgents — https://arxiv.org/html/2412.20138v7 (v7 2025-06-03)
- TradingAgents repo — https://github.com/TauricResearch/TradingAgents (2026-10)
- FinCon — https://arxiv.org/html/2407.06567v3 (v3 2024-11-07)
- FinMem — https://arxiv.org/abs/2311.13743 (2023-12-03)
- 加密货币 MAS — https://arxiv.org/html/2501.00826v3 (v3 2026-06-16)
- TiMi — https://arxiv.org/html/2510.04787v2 (v2 2026-02-09)

### F2 — 开源交易机器人工程实践

- Freqtrade: Trade 对象 — https://www.freqtrade.io/en/stable/trade-object/
- Freqtrade: 止损 — https://www.freqtrade.io/en/stable/stoploss/
- Freqtrade: 插件/Protections — https://www.freqtrade.io/en/stable/plugins/
- Freqtrade: 开发者文档 — https://www.freqtrade.io/en/stable/developer/
- Freqtrade: 回测 — https://www.freqtrade.io/en/stable/backtesting/
- Freqtrade: bot 基础 — https://www.freqtrade.io/en/stable/bot-basics/
- Hummingbot: V2 策略 — https://hummingbot.org/strategies/v2-strategies/
- Hummingbot: Executors — https://hummingbot.org/strategies/v2-strategies/executors/
- Hummingbot: Clock tick — https://hummingbot.org/client/global-configs/clock-tick/
- Hummingbot: 排障 — https://hummingbot.org/troubleshooting/
- Jesse: 入门 — https://docs.jesse.trade/docs/getting-started/
- Jesse: 实盘 — https://docs.jesse.trade/docs/livetrade

### F3 — LLM 结构化输出可靠性

- OpenAI 结构化输出公告 — https://openai.com/index/introducing-structured-outputs-in-the-api/ (2024-08-06)
- JSONSchemaBench — https://arxiv.org/abs/2501.10868 (2025-02-27)
- 格式限制损害推理 — https://arxiv.org/abs/2408.02442 (2024-10-14)
- 先草稿后约束（+24pp） — http://export.arxiv.org/api/query?search_query=abs:"constrained+decoding"+AND+abs:"structured" (arXiv 2603.03305, 2026-02-08)
- 结构雪球（structure snowballing） — https://arxiv.org/abs/2604.06066 (2026-04-07)
- 解码期闭环控制 — https://arxiv.org/abs/2603.27905 (2026-04-06)
- Instructor 重试文档 — https://python.useinstructor.com/concepts/retrying/
- HN 实践者讨论 — https://hn.algolia.com/api/v1/search?query=structured+outputs+JSON+schema+reliability&tags=story&hitsPerPage=20 (2026-03-08)

### F4 — 订单/持仓状态一致性

- NautilusTrader: execution policies — https://nautilustrader.io/docs/latest/concepts/execution/policies/
- NautilusTrader: reconciliation — https://nautilustrader.io/docs/latest/concepts/execution/reconciliation/
- NautilusTrader: execution — https://nautilustrader.io/docs/latest/concepts/execution/
- Gate.io FuturesOrder（`auto_size`/`text` 约束） — https://github.com/gateio/gateapi-python/blob/master/docs/FuturesOrder.md
- Hyperliquid exchange endpoint（`cloid`/`positionTpsl`/dead-man's switch） — https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
- FIX 4.4 ClOrdID — https://www.onixs.biz/fix-dictionary/4.4/tagnum_11.html

### F5 — LLM 交易 agent 失败模式

- FARSIGHT SoK（15 个方案审计） — https://arxiv.org/abs/2609.19705 (2026-09-17)
- 内幕交易与隐瞒（Scheurer et al.） — https://arxiv.org/abs/2311.07590 (2023-11-09)
- StockBench — https://arxiv.org/abs/2510.02209 (2025-10-02)
- KTD-Fin（知识截止污染） — https://arxiv.org/abs/2605.28359 (2026-05-27)
- AI-Trader（跨市场基准） — https://arxiv.org/abs/2512.10971 (2025-12-01)
- Agent Market Arena — https://arxiv.org/abs/2510.11695 (2025-10-13)
- ATLAS（订单感知动作空间） — https://arxiv.org/abs/2510.15949 (2025-10-10)
- 归因分析 — https://arxiv.org/abs/2607.10286 (2026-07-11)
- TradingAgents fork 前瞻偏差 — https://raw.githubusercontent.com/ralphgrewe/TradingAgents/main/LEARNINGS.md (2026-05-01)

### F6 — 风控与 kill switch

- Freqtrade: 配置 — https://www.freqtrade.io/en/stable/configuration/
- Bybit risk-limit — https://bybit-exchange.github.io/docs/v5/market/risk-limit
- Hyperliquid: margin tiers — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers.md
- Hyperliquid: margining — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining.md
- SEC 15c3-5 采纳（2010） — https://www.sec.gov/news/press/2010/2010-210.htm
- SEC Knight Capital 执法（2013） — https://www.sec.gov/news/press-release/2013-222 (2013-10-16)
- NBER WP 22208（波动率目标化） — https://www.nber.org/papers/w22208 (2016-04)

### F7 — 多 agent 辩论与共识

- 多 agent 辩论（Du et al.） — https://arxiv.org/abs/2305.14325 (2023-05-23)
- 辩论基准对比（Smit et al.） — https://arxiv.org/abs/2311.17371 (2023-11-29) + https://arxiv.org/html/2311.17371v3
- Self-consistency — https://arxiv.org/abs/2203.11171 (2022-03-21)
- 弱评判者仲裁强者（Khan et al.） — https://arxiv.org/abs/2402.06782 (2024-02-09)
- 辩论可能有害（Wynn et al.） — https://arxiv.org/abs/2509.05396 (2025-09-05)
- 问题漂移 — https://arxiv.org/abs/2502.19559 (2025-02-26)
- 法律推理辩论（L-MAD） — https://arxiv.org/abs/2607.09099 (2026-07-10)
- 谄媚/从众（Yao et al.） — https://arxiv.org/abs/2509.23055 (2025-09-27)
- Silent Dissent（隐藏自己的答案 → 从众 8%→89%） — https://arxiv.org/abs/2610.02702 (2026-10-02)
- iMAD 选择性触发 — https://arxiv.org/abs/2511.11306 (2025-11-14)
- 异质性胜过轮数（Hegazy） — https://arxiv.org/abs/2410.12853 (2024-10-10)
- Free-MAD（反从众） — https://arxiv.org/abs/2509.11035 (2025-09-14)
- LLM 评判噪声 — https://arxiv.org/abs/2606.13685 (2026-04-23)
- 前沿评判者不自洽 — https://arxiv.org/abs/2512.16041 (2025-12-17)
- Persona 宜人性 ↔ 谄媚 — https://arxiv.org/abs/2604.10733 (2026-04-12)
- 聚合替代辩论 — https://arxiv.org/abs/2607.18269 (2026-05-26)

---

*报告生成：2026-10-05 · 所有 `[n]` 引用可在 `findings/F*.md` 中逐条溯源（每条含 verbatim quote + URL + 发布日期 + 置信度）*

