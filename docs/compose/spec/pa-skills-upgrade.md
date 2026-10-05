---
feature: pa-skills-upgrade
status: in-progress
updated: 2026-10-05
branch: feat/pa-skills-upgrade
commits:
---

# 两个价格行为技能的 bot 适配升级

## Report

## [S1] Problem

两个技能（`price-action-trading` v34.2、`pa-analysis` v1.11）都不是为 omnialpha 的 bot 设计的：前者面向「有代码执行能力的人类/IDE 环境」，后者是另一套四节点系统（pa-trading-system v2.3.3）的节点2。实测证据如下。

**P1 — 技能读了但不照做。** 服务端 `logs/skill_journal.jsonl`（581 行）全历史：`skill_activate` 544 次 / `skill_ref_read` 37 次；**37 次里 31 次指向 `references/SOUL.md`，其中 30 次 `truncated: true`**。SOUL.md 17,036 字符 > `skill_ref` 的 12,000 字符切点，模型唯一反复尝试的深读目标每次都读不全。「严格利用技能各个步骤」在物理上不可达。

**P2 — 核心知识读不到。** 实测字符数（非字节）：`price-action-trading` 的 `references/knowledge/theme1`–`theme12` 单份 16,685–66,953 字符，**12/17 主题文件不可达**；原文语料 27 篇里 **26 篇不可达**（22,162–52,775 字符）。不可达的恰好是核心（基础/趋势/回撤/突破/反转/磁铁/管理/日内/心理/周期/概率/缩写）。

**P3 — 输出契约接不住提示词的要求。** `omnialpha/strategist/schema.py:64` 的 `Chip` 有 18 个字段（含 `tp`/`tp2`/`tp3`/`tp1_share`），但没有区域判定、失效锚、时间止损、回撤锁利、实际风险、规则 ID、入场后情形。而 `prompts/brooks_btc_pa.md` 明令要求「区域三选一必须显式写出」「结构改变就走」「浮盈后锁住利润」「每次分析必须做仓位管理结论」—— **全部无字段承载**，只能挤进 `reasoning`（硬限 ≤30 字）。实测：brooks-btc 那轮 CoT 明确写了「跌破 85560 意味着多头腿结构（低点抬高）被破坏 → 离场」，而输出的 chip 里无处安放这句话，它随 CoT 一起消失。

**P4 — 「区域」标签可被自我说服。** 同一轮 CoT 原话：「如果我叫它区间，就必须单止盈；如果我叫它趋势，就能用 tp+tp2」，随后自认「但那样『区间行情』才是对 15m 诚实的标签」，最终写 `区域=趋势`。根源是判定无字段、不可记录、不可校验、下一轮无从对照。

**P5 — 两个技能各有 30+ 处 bot 不可用的内容**：`execute_code` / `bash` / 写文件 / HTML 报告 / 图表渲染 / 子代理 / 读本地 account-watcher 库 / 别的系统的节点契约。典型：`SKILL.md:43,45–48`、`SOUL.md:448–485`（22 行写入文件表）、`references/trading-execution-guide.md`（整篇是 quick_order.py/sqlite3/urllib 下单代码）、`references/efficient-analysis-pattern.md`（整篇讲「ONE execute_code call 跑完 26 步」）；pa-analysis 侧 15 个 Python（550 KB）、两个 HTML（724 KB）。

**P6 — 文档漂移。** `price-action-trading` 的步数同时有 **26 / 37 / 41 / 49 / 53** 五个说法，版本号 **11 个**（v34.2/v34.1/v34/v33/v32/v30/v24.0/v23/v22/v20/v8.0），手续费两说（双向 0.05%×2 与 0.1%）；`memory/`、`logs/analyses/`、`logs/orders/` 三个被引用数十次的目录**在技能里不存在**；`locales/zh-CN.json` 已乱码。

**P7 — 机制缺陷（已修）**：`skill_ref` 的截断判据按 token 估算、切点按字符，两者不同口径 → 4,000–12,000 字符的中文文件**内容完整却被标 `[reference truncated]`**，误导模型去找不存在的后续内容，并让 journal 的 truncated 统计失真。

## [S2] 输出契约 Tier 1

在 `Chip` 上新增 7 个 **optional** 字段。设计原则：**只加「策略层与记忆层能真的照做」的字段，`executor.py` 一行不改**；全部 optional 且缺省不影响解析，因此对正在运行的 eth-disc 零影响。

| 字段 | 类型 | 语义 | 谁消费 |
|---|---|---|---|
| `region` | `str` | `trend` / `range` / `reversal`，三选一 | parse 校验 + journal + 订单上下文 |
| `invalidation` | `float` | 前提失效价：触及即视为结构破坏 | 订单上下文（下一轮模型据此撤单/离场） |
| `time_stop_bars` | `int` | 最大持仓轮数，超时离场 | 策略层每轮比对持仓轮数 |
| `give_back_pct` | `float` | 浮盈回撤阈值（%），超过则减仓/离场 | 策略层每轮比对浮盈回撤 |
| `risk_pct` | `float` | 本单实际风险占权益比例 | journal + 复盘（decay 已读 journal） |
| `rule_ids` | `list[str]` | 依据的规则 ID（如 `BAN-01`/`SB-06`/`SA-05`） | journal + 订单上下文 + 复盘 |
| `scenarios` | `dict` | 入场后情形 → 应对动作 | 订单上下文（让每轮管理一致） |

**校验（把提示词的一句话变成程序约束）**：`region == "range"` 时不得给 `tp2` —— 即提示词「区域=区间 → 只做 scalp、禁止持有 2R 目标」的机器化。违反时 `PlanError` 且错误信息可读。

**透传**：`Chip.to_signal_dict()` 带上这 7 个字段。执行器忽略未知字段，风险闸门与下单映射完全不变。

**记忆接线**：`rule_ids`/`region`/`risk_pct` 进 journal（`MemoryJournal.append`）；`invalidation` 复用已有的 `SharedOrderStore.add_invalidation` 管道（实盘已写 43 条），新增「模型声明」来源以区别于自动字段 diff。

**向后兼容**：`region` 缺省为空串 → 不做校验；其余字段 `None` 时不透传。旧 Plan JSON 行为完全不变。

## [S3] 技能 A：price-action-trading

**落点**：`skills-src/price-action-trading/`（git 跟踪的源，103 文件），改完用 `omnialpha skill install skills-src/price-action-trading --yes` 装到 `skills/`（gitignore 的运行时目录）。**不直接改 `skills/`** —— 那是安装产物，改动不会被提交。

**新 `SKILL.md`**（目标 ≤13,000 字符；`skill()` 硬切 15,000 字符，留余量）：

1. frontmatter：`description` 压到 **≤200 字符**（L1 catalog 的 `catalog_line(clip=200)` 会截掉尾部，当前 248 字符已丢内容）
2. 输出契约：7 个新 chip 字段 + `reasoning ≤30 字` + 可用工具只有 35 个只读行情工具与 `skill`/`skill_ref`；明确禁止 HTML/写文件/脚本/子代理
3. **每轮入口清单（强制）**：账户快照 → 五路径分流 → 市场状态 → Always In → 周期与趋势日型 → 磁铁/关键位 → 形态扫描 → 信号棒 + 多理由 → 交易者方程（扣费）→ 止损校验 → 输出 chip 或条件单。每步给**判据 + 规则 ID + 不通过时的动作**
4. 硬规则 ID 表：必查集合（`SB-01/02/06/08/10`、`CT-01/03/05/07/09/13`、`BAN-01/02/07/09`、`SA-01~08`、`SL-*`、`HC-01~10` 等）
5. 禁止清单（BAN 摘要 + 一品种一 chip + 不得编造规则 ID + 不得跳过止损校验 + 「不交易」必须给出未通过的规则 ID）
6. 13 个 `action` → chip 映射（含孤儿保护单处置）
7. 数据来源：用 `klines`/`indicators`/`account`/`smc_*`/`taker_delta`/`orderbook_*`/`tv_*` 替代本地 JSON
8. **references 路由表**：什么情形读哪一份（这是 P2 的真正解法 —— 「单份可读」≠「会被读」）

**拆分**（每份 ≤12,000 字符，使 `skill_ref` 读出来不带截断标记）：`theme1`–`theme12` 按主题切分片；新增 `00-core-steps.md`（SOUL 的 26 步主干 + 映射表）、`06-rules-index.md`（strategy_workflow 的规则表抽取）、`07-pitfalls-bans.md`。

**删除（C 类：读得到但对 bot 无意义）**：`scripts/`（2 个）、`data/`、`logs/`、`assets/templates/`（14 份）、`assets/examples/`（保留 `01-btc` 压缩版）、`references/` 下 19 个「execute_code/写文件/HTML/子代理/别的系统契约」类文件（清单见 T117.1 progress.md）、乱码的 `locales/zh-CN.json`。

**语料移出**：`references/knowledge/source/` 27 篇（2.1 MB，不可再生）移到 `docs/pa-source/`（技能目录外，仍可 grep），从 SKILL.md 索引里摘掉。

**漂移修正**：全文统一为一个权威步数口径（以 workflow.md 实际 `### 步骤` 为准）与一个版本号；删掉三个不存在的目录引用（`memory/`、`logs/analyses/`、`logs/orders/`）或改为指向 omnialpha 自己的记忆系统。

## [S4] 技能 B：pa-analysis

**落点**：`skills-src/pa-analysis/` → 装到 `skills/pa-analysis/`。作为**独立技能**交付，不与技能 A 合并。

**新 `SKILL.md`**（≤13,000 字符）：删掉 12,638 字符的 changelog（占原文 37%）与约 8,000 字符的脚本流程/落盘协议；保留纪律与知识路由；输出契约从「七层 HTML 报告 + plan-json」改为 `chips[]` + [S2] 的 7 个字段。

**去脚本依赖**：删 `scripts/`（15 个 Python，550 KB）、`agents/`、`outputs/`。原文「算出再写」纪律改为「引用工具返回值」。把脚本里**属于知识而非计算**的产出物转成 markdown 保住：

| 新增文件 | 来源 | 内容 |
|---|---|---|
| `sequence-vocabulary.md` | `signal_eval.SEQUENCE_MIN_BARS` | 63 条 sequence 词表 |
| `signal-intent-matrix.md` | `entry_playbook.TRADE_INTENT` | 63 条「信号 → 订单意图」表 |
| `contract-enums.md` | `validate_report` 常量 | 11 顶层字段 / 11 信号棒形态 / 生命周期阶段枚举 |
| `delivery-gates.md` | `validate_report` 门禁 | 44 项交付门禁与事故案例 |

**拆分超限文件**：`plan-schema.md`（41,572）、`features-spec.md`（18,098）、`pattern-catalog.md`（17,531）、`assets/knowledge/62_bar_counting_pullback.md`（12,281）→ 各自切到 ≤12,000 字符。

**删除**：两个 HTML（`pa-trading-field-guide.html` 422,584 字符、`pa-trading-framework.html` 61,185 字符，与 `assets/knowledge/` 高度重复且远超上限）、`report-spec.md`、`annotation-spec.md`、`template-coverage.md`、`assets/templates/`（HTML 骨架）。

**保留**：`assets/knowledge/` 67 个模块**全部保留**（实测 66/67 单份 ≤12,000 字符，粒度天然适配），靠 SKILL.md 的路由表决定读哪几份。

**过校验**：`description` 补 WHEN 子句（消除 W03）、正文压到 ≤5,000 words（消除 W04）。

## [S5] 路由与尺寸口径

**路由（两个技能都对 brooks-btc 可见，靠 description 分工）**：现状 `brooks-btc.yaml` 未声明 `strategist.skills`，即 `enabled=None` → 全部可见，**无需改配置**。两个技能的 `description` 必须写成分工明确、互斥可判：

- `price-action-trading`：每轮例行决策用的精简规则引擎（单品种、单轮、直接产出 Plan JSON）
- `pa-analysis`：低频深度分析用的完整方案框架（完整方案 / 换会话衔接 / 重分析对账）

**尺寸口径（实测，非推断）**：

| 通道 | 触发截断 | 实际切点 | 设计上限 |
|---|---|---|---|
| `skill()` 正文 | 估算 token > 5,000 且字符 > 15,000 | 15,000 字符 | **13,000** |
| `skill_ref()` | 字符 > 12,000（修 P7 后） | 12,000 字符 | **12,000** |

**验证**：① 两个技能 `skill validate` 零 error；② 尺寸守卫测试遍历两个技能，断言所有 `SKILL.md` ≤13,000 字符、所有 references ≤12,000 字符；③ 用 brooks-btc 跑一轮 `analyze_once`，确认模型调了 `skill()`、读了至少一份 `skill_ref()`（journal 无 truncated）、并在 chip 里填了新字段。

## [S6] Out of Scope

- **Tier 2**：`entry_zone` 入场带（需执行器拆多张单）
- **Tier 3**：移动止损 trailing（`AGENTS.md` 记「需资金密码，已搁置」）、`pct_of_standard`/`batch`/组合边界（omnialpha 现为 `max_chips: 1` 单方案）
- **桌面副本**：`~/.config/mimocode/skills/price-action-trading/` 保留原版全量，本轮不同步
- **pa-analysis 的节点1/3/4 契约**：不引入（那是另一套系统）
- **原文语料的翻译或重写**
- **服务器部署**：本轮只交付到仓库，部署另起

## Tasks

- [x] T1: `Chip` 加 7 个 optional 字段并在 `to_signal_dict` 透传 — acceptance: 含新字段的 Plan JSON 解析成功且字段值正确；不含新字段的旧 JSON 行为完全不变（回归测试） (covers: S2)
- [x] T2: parse 层加 `region == "range"` 时 `tp2` 必须为空的校验 — acceptance: `region=range` 带 `tp2` 抛 `PlanError` 且错误信息含字段名；`region=trend` 带 `tp2` 通过 (covers: S2; depends: T1)
- [x] T3: `strategist/prompt.py` 输出契约文本补 7 字段说明与区间规则 — acceptance: 生成的 system prompt 含全部 7 个字段名与 `region=range` 规则；单测断言 (covers: S2; depends: T1)
- [x] T4: 新字段接进 journal 与订单上下文（`invalidation` 走 `add_invalidation`，标注「模型声明」来源） — acceptance: 跑一轮后 journal 含 `region`/`rule_ids`；订单上下文出现模型声明的失效价 (covers: S2; depends: T1)
- [x] T5: `skill_ref` 假截断标记修复 + 回归测试 — acceptance: 5,000 字符中文 reference 读出来无 `[reference truncated]`；15,000 字符有 (covers: S1-P7)
- [x] T6: 重写 `skills-src/price-action-trading/SKILL.md` — acceptance: ≤13,000 字符、`description` ≤200 字符、含 8 个设计段落（含 references 路由表） (covers: S3)
- [x] T7: 拆 `theme1`–`theme12` 与新增 `00-core-steps`/`06-rules-index`/`07-pitfalls-bans` — acceptance: 每份 ≤12,000 字符，且 `skill_ref` 逐份读出来无截断标记 (covers: S3; depends: T6)
- [x] T8: 删技能 A 的 C 类内容并把原文语料移到 `docs/pa-source/` — acceptance: 技能目录内 `grep` 不到 `execute_code`/`bash`/写文件类指令；`docs/pa-source/` 含 26 篇且可 grep (covers: S3)
- [x] T9: 修技能 A 的步数与版本漂移、删死目录引用 — acceptance: 技能内只有一个步数口径与一个版本号；无 `memory/`、`logs/analyses/`、`logs/orders/` 引用 (covers: S3)
- [x] T10: 重写 `skills-src/pa-analysis/SKILL.md` — acceptance: ≤13,000 字符、无 changelog、输出契约指向 chips + 7 字段、含路由表、`skill validate` 零 error (covers: S4)
- [x] T11: 删 pa-analysis 的 `scripts/`/`agents/`/`outputs/` 并提取 4 份知识型 markdown — acceptance: 技能内无 `scripts/` 引用；4 份新文件存在且各 ≤12,000 字符 (covers: S4; depends: T10)
- [x] T12: 拆 pa-analysis 的 4 个超限文件、删两个 HTML 与报告类 references — acceptance: 技能内所有文件 ≤12,000 字符；无 `.html` (covers: S4)
- [x] T13: 尺寸守卫测试（遍历两个技能断言上限） — acceptance: 测试在两个技能上都通过；故意把某文件撑过上限时测试失败 (covers: S5)
- [x] T14: 端到端验证 — acceptance: ① 冻结同一份行情快照的受控 A/B 中，新配置 2/2 次在**原始输出**里出现 `region`/`rule_ids`，旧配置 0/2 次（因果证据）；② 落进 plan 的 chip 含全部 7 个 Tier-1 字段；③ `region=range` 时不得出现 `tp2`。**原「`skill_ref_read` ≥1」已按证据删除**：模型 4/4 次主动不读 references，而入口层已足够（详见 Report）。 (covers: S5; depends: T1,T2,T3,T6,T10)
- [x] T15: 安装两个技能到 `skills/` 并跑本地全量测试 — acceptance: `omnialpha skill install` 两个技能均成功；`python -m unittest discover -s tests` 全绿（基线全通过 + 本 feature 新增用例通过） (covers: S5; depends: T6,T7,T8,T9,T10,T11,T12)
