# AI 智能体 Skill / 插件 / MCP 体系设计调研 —— OmniAlpha 落地参考

> Generated 2026-09-30 · depth: deep · 50 sources · workspace: research/agent-skill-systems/

## Executive summary

1. **SKILL.md 已是跨厂商事实标准**：Anthropic 于 2025-10-16 发布 Agent Skills，2025-12-18 开放为 agentskills.io 标准，被 Claude Code、Cursor、Gemini CLI、GitHub Copilot、VS Code、OpenClaw、Hermes、ChatGPT/Codex 等数十客户端采纳；OpenAI GPT 退役迁移与 Microsoft 365 Copilot 也都落到「plugin 内的 SKILL.md」同一形态 [1][2][3][18][25]。OmniAlpha 应直接对齐该格式，而非自创 manifest。
2. **渐进式披露（progressive disclosure）是稳定性的核心**：启动只预载 name+description（约 100 token/skill），触发才读 SKILL.md 正文（建议 <5000 token、<500 行），bundled 脚本/参考资料按需加载；listing 预算约为 context window 的 1% [2][4][12]。这是 skill 能「打包大量方法论却不炸上下文」的根本机制。
3. **Skill ≠ MCP ≠ Plugin ≠ Subagent，分工明确**：Skill 打包「程序性工作流/方法论」，MCP 暴露「外部工具与数据接口」，Plugin 是「分发单元」（skills+agents+hooks+MCP 打包），Subagent 买「上下文隔离」；官方定位是互补而非替代 [1][7][15][29][43]。交易场景：下单/撤单/查仓走 MCP tool 形态，策略 playbook/风控 checklist 走 SKILL.md。
4. **可靠性靠契约与闸门，不靠模型自觉**：OpenAI/Microsoft 用 OpenAPI schema、域名白名单、审批弹层、风险分级（Always ask→Allow low-risk→Allow all）、Elevated Risk 标签、Lockdown Mode 做故障域隔离 [21][22][23][24]；MCP 把 human-in-the-loop、annotations 不可信、超时/审计/限流写成 MUST/SHOULD 规范义务 [9][10]。
5. **防篡改安装是当前最具体的供应链模式**：MCP Skills 扩展（SEP-2640 Final）要求每文件 SHA-256+size 清单，宿主使用前必须校验，批准绑定完整 URI+digest 集合、任一变更即撤销；512 文件/16 MiB 上限 [13][40]。OpenClaw 补齐了路径遏制（realpath 必须在配置根内）、installPolicy 失败即拒、AV 扫描、密钥只进 host 不进 sandbox [28][31]。
6. **prompt 成本必须硬预算**：OpenClaw 实测每 skill 约 24 token（97 字符）+ 字段长度，超出 `maxSkillsPromptChars` 先砍描述再截断列表 [32]；Claude Code skill body 加载后跨轮驻留，auto-compaction 回贴每 skill 前 5000 token、合计 25000 [12]。装 skill 多了会挤占行情数据——这与你之前的判断一致。
7. **交易智能体的共识红线：LLM 绝不碰交易**。AI Hedge Fund v2 明文 "The LLM never touches the trade"——agent 只产出 view/narrative，sizing 与下单是确定性代码，risk limits 是 agent 超不过的 hard gates；FinRobot 用 32 个确定性算子 + 6 个 audit 算子保证数字可溯源 [35][36][37]。skill 只能教方法论，不能获得绕过 executor/yaml 风控的权限。
8. **策略-as-配置是可行的免代码扩展点**：AI Hedge Fund 的 `AlphaModel -> Signal(conviction∈[-1,+1]+thesis)` + YAML strategy bundle、FinRobot 的 56 个 SKILL.md analyst playbook 按 desk 分组、TradingAgents 的独立 Risk/PM 节点——都验证了「人格/策略打包成文件、不改 Python」的路径 [35][36][38]。
9. **幂等/确定性回放是一手规范空白**：MCP 只区分协议错误与工具执行错误（isError），未规定重试语义；需从交易 API Idempotency-Key 与 OpenTelemetry GenAI conventions 另补 [42][47]。这是 skill 执行下单类副作用时必须自己补的领域。
10. **实地解剖（MiMo Desktop 一手实现）给出 skill 工具引擎的完整参考架构**：文件系统扫描发现（无注册）、catalog 只注入 name+description、`skill` 工具按需载入 body、Safe-YAML 无代码执行、`disable-model-invocation` 用户专属开关、独立 `validate_skill.py` 校验器、locales 展示与触发分离——这些是**要实现的引擎机制**，不是某个 skill 内容 [S1][S2][S3]。
11. **落地优先级建议**：P1 实现 skill 引擎（loader + catalog 注入 + skill 工具 + 校验器 + bot yaml 启停）；P2 再谈 skill 内容与 bundled 脚本；P3 工具绑定/懒连接。引擎本身先于任何 skill 内容。

## Background & scope

OmniAlpha 是 Gate 永续合约信号执行 + LLM 策略 monorepo，已有 20 个 function-calling 工具、prompts/ 人格体系、多人格共管、25 族指标。本次目标是为「可安装 skill」做设计决策依据，方向是向「AI 交易全方位智能体」（tools / plugins / skills / MCP）演进，本次主目标是 skill。时间框 2025-01 ~ 2026-09，受众是要落地实现的开发者/架构师。假设：skill 不得绕过 yaml 风控与 executor 约束；安装面必须人工可审计。

---

## 1. 格式标准：SKILL.md 已收敛，别自创

**形态**：skill = 含 `SKILL.md` 的目录。SKILL.md 以 YAML frontmatter 开头（必填 `name` + `description`），body 为 Markdown 指令；可选捆绑 `scripts/`（可执行代码）、`references/`（按需文档）、`assets/`（模板/查找表）[1][2]。引用其他文件用相对路径且保持「一层深」，避免嵌套引用链 [7]。

**开放标准的可移植 frontmatter 只有 6 个字段** [2][5]：
- `name`：1–64 字符，小写字母数字+连字符，必须与父目录名一致
- `description`：1–1024 字符（Claude Code listing 侧与 `when_to_use` 合计截断到 1536 字符）
- `license` / `compatibility`（≤500 字符）/ `metadata`（string→string map）/ `allowed-tools`（Experimental）

**关键约束**：Claude Code 私有扩展字段（`context: fork`、`hooks`、`paths`、`argument-hint` 等）在跨产品打包（claude.ai 上传、Skills API、package_skill.py）时会 **hard error** [6][12]。因此 OmniAlpha 的 skill 若想可移植/可分享，manifest 必须只用六字段，交易专属 gating 放 `metadata` 命名空间（如 `metadata.gate`）。

**已被多方实现验证**：OpenClaw 严格遵循 AgentSkills 规范，frontmatter 至少 name+description，正文用 `{baseDir}` 引用技能目录 [33]；Microsoft 365 Copilot 把 skill 定义为「directory with required SKILL.md + supporting resources/scripts」，并把 skill 定义为 plugin 的一种 capability [25]；OpenAI 迁移映射是「GPT instructions → plugin 内的一个 skill」[2][18]。

## 2. 渐进式披露与上下文经济（稳定性的第一原则）

三级加载 [1][2][3]：

| 层级 | 内容 | 成本 | 加载时机 |
|------|------|------|----------|
| L1 Metadata | name + description | ~100 token/skill | 启动时全量预载进 system prompt |
| L2 Instructions | SKILL.md body | 建议 <5000 token、<500 行 | 任务匹配、agent 读文件时 |
| L3 Resources | scripts/references/assets | 按需 | 需要时读取或执行（脚本可不进 context） |

**量化预算**（多源一致）：
- listing 预算 = context window 的 **1%**（可调 `skillListingBudgetFraction`），溢出时优先丢弃最少使用的 skill description [12]
- auto-compaction 时按最近调用顺序回贴每个 skill 前 **5000 token**，合计 **25000 token** [12]
- OpenClaw 每 skill 约 **97 字符 ≈ 24 token** + name/description/location 长度；超 `maxSkillsPromptChars` 先砍描述、再截断列表 [32]
- MCP 侧对照：naive 全量注入 tool 定义 ~150,000 token vs progressive discovery ~2,000 token；tool 定义占上下文 1%–5% 时应切换按需发现 [15]

**副作用**：SKILL.md body 一旦加载则**跨轮驻留**，每行都是重复 token 开销；官方边界是「procedure 进 skill，fact 进常驻 prompt/CLAUDE.md」[2][4]。对交易 bot 意味着：行情 snapshot 永远优先，skill listing 要硬上限。

**官方方法论**：先跑评测找能力缺口再增量写 skill；SKILL.md 过大就拆引用文件；互斥场景拆独立路径省 token；代码既可作可执行工具也可作文档（须标明跑还是读）；重点打磨 name/description 控制触发；用 `/skill-doctor` 找未用 skill、`claude plugin eval` + `tool_used: Skill` grader 量触发率 [1]。

## 3. Skill / MCP / Plugin / Subagent 的分工

业界没有把它们对立，而是分层互补 [1][7][15][29][43]：

| 机制 | 本质 | 成本特征 | 适用 |
|------|------|----------|------|
| **Skill (SKILL.md)** | 程序性知识/工作流打包 | 未触发近零；触发后 body 驻留 | 策略 playbook、风控 checklist、复盘流程、多步分析 SOP |
| **MCP tool/server** | 外部工具与数据接口（JSON-RPC） | tool 定义常驻（可 progressive discovery） | 下单、撤单、查持仓、拉 K 线——强 schema 确定性动作 |
| **Plugin** | 分发/打包单元（skills+agents+hooks+MCP） | 每个可自动调用组件的 name+description 每轮驻留；官方 marketplace 展示 Context cost | 需要统一安装/更新/分发时 |
| **Subagent** | 上下文隔离执行体 | description 常驻（>15k token 合计告警）；细则在 system prompt 按需载入 | 会灌爆主上下文的旁路任务（大量搜索/日志） |
| **Prompt-template** | 参数化交互模板（MCP prompts 原语） | 常驻或按调用 | few-shot、固定对话结构 |
| **Hooks** | 确定性行为强制 | 生命周期触发 | skill 影响不够时的强制闸门（「enforce deterministically」） |

**双向组合**：Claude Code `context: fork` 让 skill 在独立 subagent 里以 SKILL.md 为 prompt、无对话历史执行（指令必须自洽）；反过来 subagent 的 `skills` 字段可把 skill 全文预载为参考材料 [4][10]。前者隔离任务，后者隔离知识。

**Skill 声明依赖工具**：MCP 侧建议 skill 文件声明所需 MCP server，仅在 skill 被调用时懒连接 [15]——这正好对应 OmniAlpha 里「skill 只在需要下单时才启用 Gate API 权限」。

**官方互补定位**：「Skills 通过教会 agent 更复杂的工作流来 complement MCP servers」[1][3]。交易场景映射（F7 建议亦如此）：下单/撤单/查仓 → MCP tool 形态（强 schema、权限边界）；策略 playbook/风控 checklist/复盘 → SKILL.md（渐进披露、可捆绑回测脚本）[12]。

## 4. 加载、发现与治理（OpenClaw 是参考实现）

OpenClaw（391k stars，MIT，Foundation 501(c)(3)）把 skill 系统做成了生产级工程 [28][32][33]：

- **7 级文件系统优先级**：workspace skills > project `.agents/skills` > personal `~/.agents/skills` > managed/state > workshop > bundled/custodian > extraDirs+plugin；同名取高优先级；发现深度上限 6 层，SKILL.md 出现即停向下遍历 [28]
- **位置 vs 可见性分离**：precedence（从哪加载）与 allowlist（哪个 agent 可用）是两套控制；`agents.entries.*.skills` 非空即最终集合不与 defaults 合并，`[]` 表示该 agent 无技能 [28]
- **加载时资格过滤**（`metadata.openclaw`）：`requires.bins/anyBins/env/config`、硬 `os` 过滤、`always` 旁路、`primaryEnv` 绑定 apiKey；支持 brew/uv/npm/go/download 安装器（download 可带 sha256，响应体上限 256 MiB）[28]
- **第三方 skill = 不可信代码**：路径 realpath 必须落在配置根内；`security.installPolicy` 本地策略命令失败即拒绝（fail closed）；ClawHub 信任封 `clawhub.skill.verify.v1` + VirusTotal/ClawScan/静态分析；**密钥只注入 host 进程该 agent turn，绝不进 sandbox** [28][31]
- **Skill Workshop 提案队列**：agent 发现可复用工作时不直接写 SKILL.md，而是产出 proposal，操作员 `skills workshop list/inspect/evaluate/apply` 审批落地——「agent 草稿、人裁决」的技能自生成隔离环 [28]
- **会话快照 + 刷新触发**：会话启动时快照技能列表，watcher（250ms debounce）/Gateway 重启/节点接入触发刷新 [32]

Claude Code 的安装层级类似：enterprise managed > personal `~/.claude/skills/` > project `.claude/skills/` > 嵌套 monorepo > `--add-dir` > plugin `skills/`（命名空间 `/plugin-name:skill-name`）> claude.ai 账号同步；同名冲突优先级 enterprise > personal > project [8]。

**治理差异**：Microsoft 强调「发布 ≠ 可用」——package/registry 只完成注册分发，之后仍需 review、acquisition、assignment、enablement、connection [26]。对实盘交易 bot，这层治理不是可选项。

## 5. 可靠性与安全工程

**人类在环是规范义务**：MCP 明确「SHOULD always be a human in the loop with the ability to deny tool invocations」，tool annotations 在非受信任 server 上必须视为 untrusted [1][2]。工具安全清单把超时、审计日志、入参/出参校验、限流、敏感操作确认写成 server MUST / client SHOULD [3]。

**闭源厂商用契约与闸门，不用模型保证** [21][22][23][24][30]：
- OpenAPI schema + None/API-key/OAuth 三档鉴权 + 工作区域名校验 + 执行前 approve
- 权限分层：Always ask → Allow read → Allow low-risk → Allow all；高风险（发消息、删文件、改权限、购买、共享敏感数据）额外确认或直接拒绝
- Elevated Risk 标签（prompt injection 普遍未缓解）；Lockdown Mode 硬切出站网络阻断 exfiltration（但不阻止注入本身）
- 故障域隔离：GPT 同时只能用 apps 或 actions；workspace 若 zero action domains 则 custom actions 完全不可执行

**沙箱是 OS 级强制，且必须 fail-closed** [4][5][6][7]：
- Claude Code：macOS Seatbelt / Linux bubblewrap+socat，约束 Bash 子进程文件系统与网络边界
- 硬闸门：`failIfUnavailable`（缺依赖则拒启动）、`allowUnsandboxedCommands: false`（禁 `dangerouslyDisableSandbox` 逃逸重试）
- 凭据 deny vs mask 两档；mask 用哨兵值 + allowlist 主机代理替换真值
- **官方承认沙箱不是完整边界**：默认不查 TLS 内容，宽域名存在 domain fronting 外泄路径；更强保证需自定义 TLS 终止代理

**已知逃逸路径**（文档化，必须防）[7]：domain fronting、Unix socket（docker.sock）、宽 write 路径、`dangerouslyDisableSandbox` 重试。交易 bot 的 skill 执行环境应默认 `allowUnsandboxedCommands: false`。

**防篡改安装（SEP-2640）** [13][40]：每文件 SHA-256+size 清单，宿主使用前 MUST 校验；持久化批准绑定完整 URI+digest 集合，任一文件增删改即撤销批准；skill 内容视为不可信输入，host 侧代码执行与 allowed-tools 需逐 skill 显式批准；同名 skill 不得静默覆盖；嵌套 skill 激活前须重新同意；512 文件/16 MiB 上限；批准只绑定清单不拉取内容。

**版本化** [11]：MCP 协议版本 YYYY-MM-DD（最后一次破坏性变更日期），废弃特性至少保留 12 个月（加急 90 天）；init 协商单一版本；2026-07-28 起无状态（per-request `_meta`），dual-era 须支持新旧互操作。

**评测与 CI** [16][17]：SEP-2484 把 conformance test 作为 Final 门槛——每个 MUST/MUST NOT/SHOULD 映射到可执行 check 或文档化 exclusion，tier 评估对钉死的 conformance release 跑以避免意外回归；MCP Inspector CLI 提供稳定退出码 0–5、stderr 单行 JSON error envelope、`--stored-auth-only` 供 CI 使用。

**幂等/重试是规范空白** [42]：MCP 只区分协议错误与 `isError: true` 工具执行错误，未规定重试语义。交易类副作用必须自建：Idempotency-Key（对齐 Stripe/交易所 API 语义）、最大重试次数、确定性回放日志（OTel GenAI conventions）。

## 6. 交易智能体的策略打包实践

**FinRobot Desktop V2**（最接近的开源类比）[36][37]：
- 56 个 `SKILL.md` analyst playbook，按 desk 分类（financial-analysis 11、equity-research 9、private-equity 10、investment-banking 9、partner-lseg 8、wealth-management 6、partner-spglobal 3）
- CLI 检索入口 `finrobot skill list` / `finrobot skill search`
- **数字可溯源**：32 个确定性 Python 算子（26 估值/分析 + 6 audit）负责所有财务数字，LLM 只做推理与写作；6 个 audit 算子在报告发布前核对叙述与数字的一致性并标记 drift
- 9 个 agent 由 markdown 指令文件定义（data/analysis/modeling/synthesis/report 流水线 + bull/bear/judge 辩论 + lead 编排），改行为不改 Python；7 条 pipeline 是带 per-step validator 与 retry 的 typed step 序列

**AI Hedge Fund v2** [35]：
- 三层可插拔：FUND（allocator）→ STRATEGY（blend policy）→ MODEL = `AlphaModel.predict(ticker, date, data_client) -> Signal`（conviction ∈ [-1,+1] + thesis）
- **策略-as-配置**：`strategies/` 丢 YAML 即可捆绑现有模型 + blend policy，无需写代码
- **不可协商红线**："The LLM never touches the trade"——agent 只产出 view/narrate；确定性代码 sizing 与下单；risk limits 是 hard gates（per-position 与 gross-exposure clamps）
- 单一 `run_cycle` 覆盖 backtest/paper/live（仅 clock 与 broker 不同）；回测时对 investor agent 隐去 ticker/行业/日历防 LLM 记忆伪装 skill

**TradingAgents** [38][39]：LangGraph 把交易公司角色拆成可组合 agent（四分析师并行 + Bull/Bear 辩论 + Trader + Risk Management + Portfolio Manager 最终批准/否决）；风控与执行是独立节点而非 prompt 一段话；point-in-time 数据契约（SEC EDGAR 按 filing date、look-ahead 过滤、verified data-access contract）；结构化输出 agent + checkpoint resume + 持久 decision log + reflection 注入。

**FinAgent**（arXiv:2402.18485）[40]：把经典交易策略与专家规则作为 tool-augmented 可复用组件注入，dual-level reflection + diversified memory retrieval。

**跨框架共识**（可直接写进 OmniAlpha 的 skill 红线）：
1. LLM 只产出观点/叙述；sizing、下单、硬限额必须是模型外的确定性代码
2. point-in-time 诚实 + backtest/live 单一代码路径是策略模块可信的前提
3. 策略打包成文件（YAML/SKILL.md），不改 Python
4. 风控是独立节点/闸门，不是 prompt 里的一段话

## 6.5 实地解剖：MiMo Desktop 的 Skill 实现（一手观察）

> 本节来自对本会话所运行的 skill 系统的直接解剖（2026-09-30），非网页来源。路径均为本机绝对路径。

### 6.5.1 目录约定与发现机制 [S1]

**写入根只有两处**（目录名 = 稳定 ID，必须与 frontmatter `name` 一致）：
- 项目级：`<project>/.mimocode/skills/<id>/SKILL.md`
- 全局级：`~/.config/mimocode/skills/<id>/SKILL.md`

**品牌兼容根**（`~/.claude/skills`、`~/.codex/skills`、`~/.agents/skills`、`~/.opencode/skills`）是**只读兼容面**，由 Desktop 设置控制（默认只扫描 `~/.agents/skills`）；官方明确「不要在那里创建 skill」，写权限始终归 MiMoCode 根，跨工具迁移用**拷贝而非软链**。

**发现是纯文件系统扫描，无注册步骤**：引擎扫 project `.mimocode/{skill,skills}/**` 与全局 config 目录；新建目录后新会话自动识别。同名冲突按目录层级优先。这与 OpenClaw 的 7 级优先级扫描同构，但更简（只有两级写入根）。

### 6.5.2 Frontmatter 与安全约束 [S1][S2]

| 字段 | 必填 | 约束 |
|------|------|------|
| `name` | ✅ | kebab-case，与目录名一致；**禁含 "claude"/"anthropic"**（保留字） |
| `description` | ✅ | <1024 字符；必须含 WHAT + WHEN（触发句）；**禁止 XML 尖括号**（会注入 system prompt） |
| `version` / `tags` / `icon` | 可选 | 展示与检索用 |
| `license` / `compatibility` | 可选 | compatibility 1–500 字符 |
| `allowed-tools` | 可选 | 收敛工具面（如 `"Bash(python:*) WebFetch"`） |
| `metadata` | 可选 | 任意 string→string（可放交易 gating） |
| `disable-model-invocation` | 可选 | `true` = 用户专属：模型看不到（不进 catalog、skill_search 不返回、skill 工具拒绝），但 `/skill-name` 仍可由用户触发 |

**安全设计**（可直接借鉴到 OmniAlpha）：
1. **Safe-YAML 解析，无代码执行**——frontmatter 解析器只取 key:value，不 eval
2. **frontmatter 禁 XML 尖括号**——因为它被注入 system prompt，防 prompt injection 伪装标签
3. **保留字命名禁令**——防冒充官方 skill
4. **`permission.skill` deny 规则**——授权与可达性分离：deny 使 skill 对所有人不可用（含用户）
5. **`disable-model-invocation`** 与授权正交：控制的是「模型是否能自主触发」，不是「能否使用」——长流程/有副作用的 skill（部署、下单）应设为用户专属

### 6.5.3 三级渐进披露（与官方标准一致）[S2][S3]

| 层 | 载体 | 加载时机 |
|----|------|----------|
| L1 | frontmatter（name+description） | 始终在 system prompt 的 skill catalog（`<available_skills>`） |
| L2 | SKILL.md body | `skill` 工具调用或用户 `/skill-name` 时整体注入为「附加指令」 |
| L3 | references/ scripts/ assets/ | body 里显式引用（`Read`/执行）才加载 |

关键机制：**skill body 加载后成为该回合的行为指令**（"A skill's body becomes additional instructions for the scope of that invocation"），但不改变工具面——工具集是独立的。

### 6.5.4 校验器（validate_skill.py）[S3]

独立 Python 脚本，退出码 0=PASS / 1=FAIL / 2=用法错误。检查项：

- **ERROR**：目录名非 kebab-case；SKILL.md 缺失或大小写不对；skill 目录内放 README.md；frontmatter 缺失/畸形；frontmatter 含 `<` `>`；缺 name/description；name 非 kebab 或含保留字；description >1024；compatibility 超 500
- **WARNING**：name 与目录名不一致；description <40 字符（太模糊触发不了）；description 无 "Use when" 类触发句；body >5000 词（应拆到 references/）；SKILL.md 引用的 `scripts/` `references/` `assets/` 路径不存在

这正是 MCP SEP-2484「conformance test 作为 Final 门槛」[16] 的轻量落地——**每个 MUST 有可执行 check**。

### 6.5.5 展示元数据与 i18n 分离 [S1]

`locales/zh-CN.json` 与 `locales/en-US.json` 只含两个字段：

```json
{ "displayName": "价格行为交易", "brief": "Al Brooks 价格行为交易分析、计划与复盘" }
```

规则：**displayName/brief 是给人看的 UI 文案；name/description 是给引擎匹配用的标识与触发条件**——两者永不互译、不重复。这解决了「触发语言 vs 展示语言」的冲突（description 用单一语言写触发词，UI 文案本地化）。

### 6.5.6 生产级交易 skill 样本：price-action-trading v34.2 [S4]

本机 `~/.config/mimocode/skills/price-action-trading/` 是一个**已在实盘分析中使用的 Al Brooks 价格行为 skill**，对 OmniAlpha 极有参考价值：

```
price-action-trading/
├── SKILL.md                 # v34.2，frontmatter 含 version/tags/license
├── references/              # 按需知识
│   ├── SOUL.md              # AI 角色 + 26 步流程图 + workflow×strategy 映射
│   ├── knowledge/           # 16 个主题知识（趋势/区间/反转/磁吸/心理…）+ source/ 原文分卷
│   ├── history/             # v25–v29 升级计划归档
│   └── *.md                 # 分析工作流、止损框架、pitfalls、执行指南等 30+ 文档
├── scripts/                 # 确定性脚本
│   ├── analyze_market_state.py
│   └── generate_analysis.py
├── assets/
│   ├── templates/           # trade_plan / trade_review / trading_journal / checklist / htf_analysis … 14 个模板
│   └── examples/            # 5 个真实品种分析样例（BTC/ETH/SOL/XAU/XAG）
├── data/                    # 行情快照 JSON（btc_usdt_{5m,15m,1h,4h,1d} 等）
├── logs/
│   ├── analyses/            # 带时间戳的分析产物
│   └── reports/             # market_state 报告
└── memory/                  # ★ 分层记忆（与 OmniAlpha 四层记忆同构）
    ├── L2_daily/  L3_weekly/  L4_monthly/  archive/
    ├── market_state.md  market_wisdom.md  pattern_effectiveness.md
    ├── error_patterns.md  strategy_hypotheses.md  trader_profile.md
    └── cross_instrument.md  improvement_tracker.md
```

**可直接抄的设计点**：

1. **description 含正触发 + 负触发**：「Use when 用户要求分析 BTC/ETH/SOL/XAU 等 K 线或图表截图、制定或检查交易计划…或提到 Al Brooks、price action…；**Do not use for** 基本面选股、荐币、新闻面交易、量化回测或非 K 线策略」——负触发显著降低误触发
2. **强制顺序入口**：body 开头规定「先读 references/SOUL.md → 同时用 knowledge/workflow.md（走到哪一步）+ strategy_workflow.md（这一步怎么做）」——用文档路由替代把所有知识塞进 body
3. **六层结构分层**：知识（references）/ 确定性代码（scripts）/ 模板（assets）/ 输入数据（data）/ 产物（logs）/ 记忆（memory）各归其位
4. **记忆分层 L2/L3/L4**（日/周/月）+ archive——与 OmniAlpha 的 agent-memory 四层（Order/Journal/Profile/Working）理念一致，可互操作
5. **模板化产物**：trade_plan、trade_review、trading_journal 等 14 个模板保证输出结构一致（便于机器校验与回测对齐）
6. **版本化**：frontmatter `version: "34.2"` + `references/history/` 归档升级计划——可回滚、可审计
7. **脚本与文档分离**：`analyze_market_state.py`（确定性计算）与叙述分离，呼应 FinRobot「Numbers are code-calculated」[37]

### 6.5.7 与开放标准的对照

| 维度 | agentskills.io 标准 [2] | MiMo Desktop [S1] | 一致性 |
|------|------------------------|-------------------|--------|
| 目录+SKILL.md | ✅ | ✅ | 一致 |
| name/description 必填 | ✅ | ✅ | 一致 |
| name=目录名 kebab-case | ✅ | ✅（校验器强制） | 一致 |
| description ≤1024 | ✅ | ✅（校验器强制） | 一致 |
| 可移植 6 字段 | name/description/license/compatibility/metadata/allowed-tools | 同左 + 私有扩展（version/tags/icon/disable-model-invocation） | 私有字段不跨包 |
| scripts/references/assets | ✅ | ✅ | 一致 |
| 渐进披露三级 | ✅ | ✅ | 一致 |
| frontmatter 禁 XML 尖括号 | —（未见明文） | ✅ 强制 | MiMo 更严 |
| 保留字命名禁令 | — | ✅ claude/anthropic | MiMo 更严 |
| locales 展示分离 | — | ✅ displayName/brief | MiMo 独有 |
| 用户专属开关 | Claude Code `disable-model-invocation` [6] | ✅ 同名 | 一致 |
| 校验器 | package_skill.py（打包侧） | ✅ validate_skill.py（独立） | 互补 |

**结论**：MiMo 的实现是 Agent Skills 标准的一个**严格超集**（多了安全约束、展示分离、独立校验器），可移植面仍是标准六字段。OmniAlpha 采用该形态即可同时兼容两端。

## 7. 对 OmniAlpha 的设计建议 —— **skill 工具功能（引擎）**

> 目标是实现「可安装 skill 的运行时能力」：发现 → 校验 → catalog 注入 → 按需加载 → 启停治理。skill 内容（策略 playbook）是后续填充物，不是本次交付物。

### 7.0 引擎总体架构

```
config/skills/ 或 <root>/skills/          ← skill 安装根
        │  scan（会话启动 + 配置变更）
        ▼
┌─────────────────── omnialpha/skillkit/ ───────────────────┐
│  loader.py      发现 + frontmatter 解析（Safe-YAML）       │
│  registry.py    SkillRegistry：id → meta + 本 bot 可见集    │
│  budget.py      listing token 预算（硬上限 + 溢出降级）      │
│  validate.py    安装期校验（对齐 validate_skill.py）        │
│  tool.py        LLM 工具定义 + run_tool 实现               │
└──────────────────────────────────────────────────────────┘
        │                              │
        ▼                              ▼
 system prompt 的               function calling
 <skill_catalog>                skill(name) → 注入 body
（仅 name + description）        （L2 按需加载）
        │                              │
        └──────────► LLM strategist ◄──┘
                          │
                          ▼
                   Plan JSON（advisory）
                          │
                          ▼
              executor + yaml 风控（不变，skill 碰不到）
```

### 7.1 skill 目录约定（loader 输入）

```
skills/<skill-id>/
├── SKILL.md          # 必填；frontmatter + Markdown 指令
├── references/       # 可选；按需文档
├── scripts/          # 可选；确定性脚本（P2 再执行）
└── assets/           # 可选；模板/查找表
```

Frontmatter（loader 只认这些，其余丢弃）：

| 字段 | 必填 | 引擎行为 |
|------|------|----------|
| `name` | ✅ | 稳定 ID；必须=目录名 kebab-case；禁保留字 |
| `description` | ✅ | 进 catalog 供模型匹配；≤1024 字符；必须含 WHEN 触发句；禁 XML 尖括号 |
| `allowed-tools` | 可选 | 该 skill 激活时的工具白名单（收窄而非扩大） |
| `metadata` | 可选 | string→string；放 `metadata.gate.risk_level` / `metadata.tools` 等 |
| `disable-model-invocation` | 可选 | `true`=用户/CLI 专属，模型不可自主触发 |
| `license` / `compatibility` / `version` | 可选 | 元信息；跨包分发时前两个是标准字段 |

**解析必须是 Safe-YAML（无代码执行）**，frontmatter 禁 `<` `>`（防 prompt 注入伪装标签）[S2]。

### 7.2 发现与 catalog 注入（L1）

- **扫描根**：`<root>/skills/`（全局）+ 可选 `.mimocode/skills/`（项目）；纯文件系统扫描，**无注册步骤** [S1]
- **启动时**把每个可见 skill 的 `name + description` 注入 system prompt（`<skill_catalog>` 段）；**body 永不预载**
- **每 bot 可见性**：bot yaml `skills: [brooks-pa, smc-sniper]`；`[]` = 不可见；非空即最终集（不与默认合并）[28]
- **刷新**：会话启动快照 + config 变更刷新（OpenClaw 模式 [32]）

### 7.3 skill 工具（L2，核心交付物）

在现有 20 个工具旁新增第 21 个：

```json
{
  "name": "skill",
  "description": "Load a skill's full instructions by name. Use when the task matches a skill in the catalog.",
  "parameters": { "name": "skill id (kebab-case)" }
}
```

`run_tool("skill", {"name": "brooks-pa"})` 的行为：
1. 查 registry：不存在 → 报错并列出可用 id
2. 校验可见性：该 bot 未启用 → 拒绝
3. 读 `SKILL.md` body（**L2 注入本回合为附加指令**，不改工具面）[S2]
4. 按 body 中的显式引用再读 `references/xxx.md`（L3，由模型自己 Read）
5. 写审计日志（skill_id、回合、token 数）→ 对齐 housekeeping.jsonl

**预算硬闸**（budget.py）：
| 项 | 建议 | 依据 |
|----|------|------|
| catalog 总预算 | context 的 1%，硬上限 2k token | [12][32] |
| 溢出降级 | 先砍 description，再截断列表 | [32] |
| 单 skill body | <5000 token；超则 WARNING（validate 拦） | [2] |
| body 驻留 | 跨轮驻留是常态成本——catalog 只放高频 skill | [4] |

### 7.4 安装期校验（validate.py，fail-closed）

对齐 `validate_skill.py` [S3]，退出码 0/1/2：

- **ERROR（拒装）**：非 kebab-case；SKILL.md 缺失/大小写不对；目录内 README.md；frontmatter 缺失/畸形；含 `<` `>`；缺 name/description；name 含保留字；description >1024
- **WARNING（可装）**：name≠目录名；description <40 字符；无 "Use when" 触发句；body >5000 词；引用文件不存在
- **安装流程**：`python -m omnialpha skill install <path>` → 校验 → 复制进 `skills/` → **不自动启用**（发布≠可用 [26]）→ bot yaml 手动加 id

### 7.5 启停与权限治理

1. **模型可达性与授权正交** [S2]：
   - `disable-model-invocation: true` → 模型不可自主触发（catalog 不列、skill 工具拒绝），但 CLI/用户可跑（`omnialpha skill run <id>`）
   - `permission.skill: deny`（配置层）→ 对所有人不可用
   - bot yaml `skills: []` → 该 bot 无 skill
2. **skill 一律 advisory**：body 只能影响 Plan JSON 的观点/指标选择，**无任何工具权限扩张**；`allowed-tools` 只能收窄 [S2]。executor + yaml 风控是唯一执行闸门（"LLM never touches the trade" [35]）
3. **有副作用的 skill（部署、下单辅助）必须 `disable-model-invocation: true`**
4. **密钥不进 skill**：skill 目录永不出现 OPENAI/GATE 密钥；脚本执行时 env 由 host 注入 [28]

### 7.6 引擎的可靠性红线

- frontmatter Safe-YAML、禁尖括号、保留字禁令（prompt 注入面）[S1][S2]
- skill body 里出现的路径必须在 skill 目录内（realpath 遏制）[28]
- 每次 skill 激活写审计 journal（skill_id / bot / 回合 / token）——可回放
- 评测：每个 skill 有 with/without 的触发率与 token 开销基准 [1]；validate 是 conformance check [16]
- 幂等：skill 工具本身是只读注入，无副作用；真正要幂等的是 P2 的 scripts（Idempotency-Key 缺口见 Open questions）

### 7.7 分阶段路线（引擎优先）

| 阶段 | 交付物 | 退出门槛 |
|------|--------|----------|
| **P1 skill 引擎** | `omnialpha/skillkit/`（loader/registry/budget/validate/tool）+ bot yaml `skills:` + `skill` 工具进 NATIVE_TOOLS + 审计日志 | 装 1 个测试 skill：catalog 只见 name+desc、skill 工具能载入 body、token 在预算内、validate 拦住坏包 |
| **P2 内容与脚本** | 第一个真实策略 skill；scripts/ 执行（超时+审计） | 触发准确（正/负触发）；数字可溯源 |
| **P3 工具绑定** | `metadata.tools` + allowed-tools 收窄；懒连接 | 权限分级；逐 skill 批准 |
| **P4 分发** | 打包、SHA-256 manifest、信任封 | fail-closed 安装；发布≠可用 |

**不建议**：P1 就做代码执行/自动装包/skill 直接下单/body 常驻 prompt。

## Comparison table

| 维度 | SKILL.md 指令包 | MCP tool/server | Plugin 打包 | Subagent | Prompt-template |
|------|-----------------|-----------------|-------------|----------|-----------------|
| 本质 | 程序性工作流 | 外部工具接口 | 分发单元 | 上下文隔离体 | 参数化模板 |
| 上下文成本 | L1~100t；body 触发后驻留 | 定义常驻（可 progressive） | 组件 name+desc 每轮驻留 | desc 常驻 + 独立窗口 | 常驻/按调用 |
| 可组合性 | 高（声明依赖工具） | 高（schema 组合） | 聚合 | 嵌套 | 低 |
| 可靠性杠杆 | bundled 确定性代码 | schema/超时/幂等 | 契约+治理 | 隔离故障 | 弱 |
| 权限边界 | allowed-tools（逐 skill 批准） | annotations+人工确认 | 安装不绕 OAuth | 权限收敛 | 无 |
| 交易适配 | 策略 playbook、风控 checklist | 下单/撤单/查仓 | 一揽子安装 | 大规模分析旁路 | few-shot |
| 代表 | agentskills.io、FinRobot playbooks | MCP、Gate API | OpenAI plugin、Claude plugin | Claude Code context:fork | MCP prompts |
| 来源 | [1][2][36] | [5][9][15] | [20][43] | [10][44] | [6] |

## Open questions

1. **幂等与确定性回放**：MCP/Agent Skills 一手规范未规定重试语义；需对照 Stripe/交易所 Idempotency-Key 与 OTel GenAI conventions 另立约定 [42]。skill 若捆绑下单类脚本，这是硬缺口。
2. **AutoGPT plugins**：旧 marketplace 仓库 404，未取得可信 2025–2026 一手 manifest 文档；该体系的教训（为何衰落）本次未覆盖。
3. **Google Gemini function calling / skills**：官方文档 fetch 失败，稳定性实践未覆盖；Gemini CLI 已采纳 Agent Skills [3] 但实现细节未知。
4. **独立第三方对比评测**：未找到同行评议的「五格式」基准；对比证据主要来自厂商一手文档，存在立场偏向风险。
5. **50+ skill / 20+ tool 的实测 token 预算**：官方给出公式与阈值，但无真实交易 bot 规模的压测数据；落地时应用 `/skill-doctor` 类工具实测。
6. **OWASP Agentic AI 具体控制项**：本次未拿到条目级内容；tool permission 与 human approval 的合规映射待补。

## Sources

[1] Equipping agents for the real world with Agent Skills — https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills (published 2025-10-16, accessed 2026-09-30)
[2] Agent Skills Specification — https://agentskills.io/specification (published 2025-12-18, accessed 2026-09-30)
[3] Agent Skills open standard — https://agentskills.io/ (published 2025-12-18, accessed 2026-09-30)
[4] Claude Code: Skills — https://code.claude.com/docs/en/skills (live docs, accessed 2026-09-30)
[5] MCP Architecture (2025-06-18) — https://modelcontextprotocol.io/specification/2025-06-18/architecture (published 2025-06-18, accessed 2026-09-30)
[6] MCP Server primitives (2025-06-18) — https://modelcontextprotocol.io/specification/2025-06-18/server (published 2025-06-18, accessed 2026-09-30)
[7] Claude Code: Sandboxing — https://code.claude.com/docs/en/sandboxing (live docs, accessed 2026-09-30)
[8] MCP specification versions — https://modelcontextprotocol.io/llms.txt (published 2026-07-28, accessed 2026-09-30)
[9] MCP Tools (2025-06-18) — https://modelcontextprotocol.io/specification/2025-06-18/server/tools (published 2025-06-18, accessed 2026-09-30)
[10] MCP Lifecycle (2025-06-18) — https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle (published 2025-06-18, accessed 2026-09-30)
[11] MCP versioning (2025-06-18) — https://modelcontextprotocol.io/docs/2025-06-18/learn/versioning (published 2025-06-18, accessed 2026-09-30)
[12] Claude Code: Skills (context budget sections) — https://code.claude.com/docs/en/skills (accessed 2026-09-30)
[13] MCP Skills extension (SEP-2640) — https://modelcontextprotocol.io/extensions/skills/overview (Final, accessed 2026-09-30)
[14] MCP changelog 2026-07-28 — https://modelcontextprotocol.io/specification/2026-07-28/changelog.md (published 2026-07-28, accessed 2026-09-30)
[15] MCP client best practices — https://modelcontextprotocol.io/docs/2026-07-28/develop/clients/client-best-practices.md (published 2026-07-28, accessed 2026-09-30)
[16] MCP SEP-2484 conformance tests — https://modelcontextprotocol.io/seps/2484-conformance-tests-required-for-final-seps.md (published 2026-03-27, accessed 2026-09-30)
[17] MCP Inspector CLI — https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector/cli (published 2026-07-28, accessed 2026-09-30)
[18] Custom GPT retirement and migration FAQ — https://help.openai.com/en/articles/20001519-custom-gpt-retirement-and-migration-faq (updated 2026-09, accessed 2026-09-30)
[19] Skills in ChatGPT — https://help.openai.com/en/articles/20001066-skills-in-chatgpt (updated 2026-08/09, accessed 2026-09-30)
[20] Plugins in ChatGPT — https://help.openai.com/en/articles/20001256-plugins-in-chatgpt (updated 2026-09-29, accessed 2026-09-30)
[21] Configuring actions in GPTs — https://help.openai.com/en/articles/9442513-configuring-actions-in-gpts (updated 2026-09-28, accessed 2026-09-30)
[22] Managing app permissions in ChatGPT — https://help.openai.com/en/articles/20001495-managing-app-permissions-in-chatgpt (updated 2026-09-18, accessed 2026-09-30)
[23] Elevated Risk labels — https://help.openai.com/en/articles/20001062-elevated-risk-labels (updated 2026-08-31, accessed 2026-09-30)
[24] Lockdown Mode — https://help.openai.com/en/articles/20001061-lockdown-mode (updated 2026-09-28, accessed 2026-09-30)
[25] Microsoft 365 Copilot plugin type: skills — https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-type-skills (ms.date 2026-09-30, accessed 2026-09-30)
[26] Microsoft 365 Copilot plugins overview — https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugins-overview (2026-09-30, accessed 2026-09-30)
[27] Microsoft 365 Copilot plugin type: MCP servers — https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-type-mcp-servers (2026-09-30, accessed 2026-09-30)
[28] OpenClaw: Skills — https://docs.openclaw.ai/tools/skills (live docs, accessed 2026-09-30)
[29] LangChain Tools — https://docs.langchain.com/oss/python/langchain/tools (live docs, accessed 2026-09-30)
[30] LangChain Tools (wrap_tool_call) — https://docs.langchain.com/oss/python/langchain/tools (accessed 2026-09-30)
[31] OpenClaw security/installPolicy — https://docs.openclaw.ai/tools/skills (accessed 2026-09-30)
[32] OpenClaw skill prompt budget — https://docs.openclaw.ai/tools/skills (accessed 2026-09-30)
[33] OpenClaw GitHub — https://github.com/openclaw/openclaw (2026-09 snapshot: 391k stars, accessed 2026-09-30)
[34] CrewAI — https://github.com/crewAIInc/crewAI (59.2k stars, accessed 2026-09-30)
[35] AI Hedge Fund (hedge_fund) — https://github.com/virattt/ai-hedge-fund/tree/main/hedge_fund (accessed 2026-09-30)
[36] FinRobot Desktop V2 README — https://github.com/AI4Finance-Foundation/FinRobot/blob/master/finrobot_desktop/README.md (accessed 2026-09-30)
[37] FinRobot — https://github.com/AI4Finance-Foundation/FinRobot (accessed 2026-09-30)
[38] TradingAgents — https://github.com/TauricResearch/TradingAgents (v0.5.x 2026-09, accessed 2026-09-30)
[39] TradingAgents paper — https://arxiv.org/abs/2412.20138 (v7 2025-06-03, accessed 2026-09-30)
[40] FinAgent — https://arxiv.org/abs/2402.18485 (v3 2024-06-28, accessed 2026-09-30)
[41] FinGPT — https://github.com/AI4Finance-Foundation/FinGPT (accessed 2026-09-30)
[42] MCP Security Best Practices — https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices (published 2025-06-18, accessed 2026-09-30)
[43] Claude Code: Plugins overview — https://code.claude.com/docs/en/plugins/overview (accessed 2026-09-30)
[44] Claude Code: Sub-agents — https://code.claude.com/docs/en/sub-agents (accessed 2026-09-30)
[45] smolagents — https://github.com/huggingface/smolagents (29.6k stars, accessed 2026-09-30)
[46] Hermes Agent — https://hermes-agent.nousresearch.com/ ; https://hermes-agent.nousresearch.com/docs/skills (accessed 2026-09-30, confidence medium)
[47] MCP design principles — https://modelcontextprotocol.io/community/design-principles.md (accessed 2026-09-30)
[48] Claude Skills announcement — https://claude.com/blog/skills (published 2025-10-16, accessed 2026-09-30)
[49] Building effective agents — https://www.anthropic.com/engineering/building-effective-agents (published 2024-12-19, accessed 2026-09-30)
[50] MCP Registry — https://modelcontextprotocol.io/registry/about.md (accessed 2026-09-30)

**一手实地观察（本机，2026-09-30）**

[S1] MiMo Skill Authoring — `C:\Users\w6485\AppData\Roaming\Xiaomi MiMo\engine-config\skills\mimo-skill-authoring\SKILL.md`（目录约定 / locales 展示分离 / 品牌兼容根只读；本地一手）
[S2] Skill Creator frontmatter reference — `C:\Users\w6485\.local\share\mimocode\builtin_skills\desktop-1579e7d\skills\skill-creator\references\frontmatter.md`（必填/可选字段、disable-model-invocation 表、安全限制、description 正反例；本地一手）
[S3] validate_skill.py + skill-creator SKILL.md — `C:\Users\w6485\.local\share\mimocode\builtin_skills\desktop-1579e7d\skills\skill-creator\scripts\validate_skill.py`（校验项与退出码；本地一手）
[S4] price-action-trading v34.2 — `C:\Users\w6485\.config\mimocode\skills\price-action-trading\`（生产级交易 skill 六层结构 / 负面触发 / L2-L4 记忆 / 14 模板；本地一手）
