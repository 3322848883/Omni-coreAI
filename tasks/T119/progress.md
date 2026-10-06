# T119 — `pa-analysis` 技能改造复核与收口（T10 / T11 / T12）

> 工作区：`C:\Users\w6485\Desktop\测试\omnialpha\.worktrees\pa-skills-upgrade`
> 技能落点：`skills-src/pa-analysis/`
> 依据：`docs/compose/spec/pa-skills-upgrade.md` 的 [S4]/[S5]，任务 T10 / T11 / T12

---

## 目标

把 `pa-analysis`（原为 pa-trading-system v2.3.3 的「节点2」，为**有代码执行能力**的 Trae/Codex 环境设计）改造为 omnialpha 的独立 bot 技能：

1. **[T10]** 重写 `SKILL.md`：删 changelog 与脚本流程，输出契约从「七层 HTML 报告 + 内嵌 plan-json」改为 `chips[]` + [S2] 的 7 个字段（`region`/`invalidation`/`time_stop_bars`/`give_back_pct`/`risk_pct`/`rule_ids`/`scenarios`），补 references 路由表，正文 ≤13,000 字符、消除 W03/W04。
2. **[T11]** 去脚本依赖：删 `scripts/`/`agents/`/`outputs/`，并把脚本里**属于知识而非计算**的产出物转成 4 份 markdown（各 ≤12,000 字符）。
3. **[T12]** 拆分 4 个超限文件（各 ≤12,000 字符），删两个 HTML 与报告类 references；保留 `assets/knowledge/` 全部 67 模块。

**硬约束**：`SKILL.md` 正文 ≤13,000 字符；`description` ≤1024 字符且含 WHEN 子句；每一份 references/assets 文件 ≤12,000 字符；全程按**字符数**判断。

---

## 已完成

### 0. 状态确认（重要）

进入任务时，**T10–T12 已被前序代理实现并提交**（commit `80bf610 feat(skill): pa-analysis 改造为独立 bot 技能（T10-T12）`，其后 `478f4d9` 加了尺寸守卫）。本次工作 = **逐条复核验收标准 + 补齐实际缺口**，未推翻已完成的合理部分。

### 1. 已核实达标的部分（未改动）

| 验收项 | 实测 |
|---|---|
| `scripts/`（15 py）/`agents/`/`outputs/` 已删 | 三者均不存在 |
| 两个 HTML 已删 | `assets/guides/`（`pa-trading-field-guide.html` 422,584 字符、`pa-trading-framework.html` 61,185 字符）不存在 |
| 报告类 references 已删 | `report-spec.md`、`annotation-spec.md`、`template-coverage.md` 均不存在 |
| `assets/templates/` 已删 | 不存在 |
| `assets/knowledge/` 保留 | 67 模块全在（68 文件，因 62 号拆为 2 片） |
| 4 份超限文件已拆 | `plan-schema.md`→`_part1..6`、`features-spec.md`→`_part1..2`、`pattern-catalog.md`→`_part1..2`、`62_bar_counting_pullback.md`→`_part1..2` |
| 4 份新知识 markdown 已建 | `sequence-vocabulary.md`、`signal-intent-matrix.md`、`contract-enums.md`、`delivery-gates.md` |
| `SKILL.md` 输出契约 | 已指向 `chips[]` + 7 字段；含 references 路由表；无 changelog；无「双骨架 fill-in」/「validate_report 门禁」/HTML 报告 |
| 尺寸 | `SKILL.md` 正文 7,055 字符；最大文件 11,370 字符（`plan-schema_part6.md`） |

### 2. 本次补齐的缺口（共 29 个文件被修改，+391 / −412 行）

**(a) T11 知识丢失 —— `signal-intent-matrix.md` 两列 63 行全空**

原表 `TRADE_INTENT` 每条含 `order`/`dir`/`trade`/`k_rule`/`e_rule` 五个字段；提取后的文件只保留了 `order`，`direction` 与 `note` 两列**63 行全为空**，`dir`/`trade`/`k_rule`/`e_rule` 四类知识（信号K 定位、入场K 挂法、方向语义、风格）**全部丢失**。这违反 T11 的「保留原表的结构与全部条目」。

→ 重建为 6 列全表（`sequence | order | dir | 风格 | 信号K | 入场K`），63 条逐条回填，并补「`order=none` 的 20 条是结构背景类、不得单独构成入场触发」「突破族统一 stop」的读法说明。

**(b) T11 知识丢失 —— `sequence-vocabulary.md` 丢掉判据注记**

63 条条目数正确，但原 `SEQUENCE_MIN_BARS` 每条尾部的**语义注释**（如 `ii`=连续 2 根内包、`breakout_ignition`=需前 20 根均实体/均量基线、`hs_top`=需 ≥5 摆点）全部丢失。

→ 补回 `判据注记` 列，63 条逐条回填。

**(c) 验证项 3 不达标 —— `scripts/` 与 `.py` 引用残留 83 处 / 18 个文件**

这是本次最实质的缺口。前序实现把 `scripts/` 目录删了，但**正文里仍在引用脚本与脚本命令**，例如：

- `analysis-workflow.md`：九阶段 state 机 + `python -B scripts/analysis_preflight.py --set-phase`、`scripts/plan_pulse.py`
- `quality-checklist.md`：`scripts/chart_svg.py`、`scripts/selfcheck_contract.py`、`features.py 原语 / signal_eval.py 词表 / plan_loader.py 档位`
- `verification-checklist.md`：`运行 scripts/validate_report.py`、`scripts/plan_pulse.py`
- `delivery-gates.md`：`python validate_report.py <报告路径.html>`
- `features-spec_part1/2.md`：`features.py`（28 处）、`python visualize_features.py --symbol ...`
- `plan-schema_part1..6.md`、`pattern-catalog_part2.md`、`51_entry_rules.md`：`validate_report.py` / `signal_eval.py` / `entry_playbook.py` / `quick_order.py`

→ 全部消除（详见 §「已完成 3」）。

**(d) 21 处悬空路径**（指向已删或已改名文件，bot 会 `skill_ref` 失败）

- `references/plan-schema.md`（已拆）→ `_part1` / `_part3`（按节归属）
- `references/pattern-catalog.md`（已拆）→ `_part1` / `_part2`（第五节在 part2）
- `references/annotation-spec.md`（已删）→ 删引用
- `assets/templates/plan-skeleton.json` / `plan-shell.html`（已删）→ 改述
- **11 个知识模块**引用的 `pa-trading-field-guide.html`（已删，422,584 字符）→ 改为「场记指南（原文语料，未随技能打包）」，保住「本模块蒸馏自场记指南第 XX 章」的溯源语义

**(e) 三个文件从「报告/脚本驱动」改写为 bot 原生**

| 文件 | 原状 | 改后 |
|---|---|---|
| `analysis-workflow.md` | 九阶段 state 机 + `--set-phase` 原子更新 + `outputs/` 落盘 + `annotation-spec.md`/`chart_svg.py` 绘图链 | 「无脚本版」工作流：取数用只读工具、判读由模型做；**「算出再写」→「引用工具返回值」**；交接对账改为读订单上下文；删除图表/报告/state 相关全部内容 |
| `quality-checklist.md` | 含「报告覆盖 7 层」「内嵌 `<script>`」「54 板块覆盖对照」「八、标注（图源/时间锚定/拆图）」等整节 | 删图表/报告/模板节；「交付程序门禁」改为「输出前自检（对应 plan 层 29 项）」；补 `region=range` 禁 `tp2`、`scenarios` 唯一 id、逆 AIL 结构互斥 |
| `verification-checklist.md` | A6 = 「运行 `scripts/validate_report.py`，29 项全 PASS」，核对结果写入「报告数据复核记录」节 | A6 改为**自检**（对照 `delivery-gates.md` plan 层 29 项）；核对结果写进 `rule_ids` 说明 / `scenarios`；保留 A1–A5 与四类事故案例 |

**(f) `features-spec_part1.md` 结构改造**

删掉「六、人工复核工具（`visualize_features.py` 交互复核页 + `file://` 打开）」整节；把「七、分层特征加载（`features_summary.py` CLI）」改写成「六、分层阅读顺序（先窗口后逐K）」——**保留 L0/L1/L2 三层知识**（窗口摘要 → 近 20 根逐K → 近 5 根摘要），去掉 CLI 与落盘；标题 `features.py 概念对照规范（程序辅助层契约）` → `几何特征口径规范（AI 自算对照）`。

### 3. 引用净化的做法（可审计）

先用「上下文优先 + 长串优先」的替换表处理 83 处（`pa-executor/core/signal_eval.py` → `执行侧序列判定` 等），再逐行复查消除替换残留（`执行侧 执行侧`、`交付门禁门禁`、双空格等 22 处），最后做全文体检确认 **0 残留**。所有替换保留原意，只把「脚本/文件路径」换成「执行侧概念」。

### 4. 临时脚本（已删）

`scripts/_szcheck_pa.py`、`scripts/_fix_pa_refs.py`、`scripts/_fix_pa_audit.py` —— 均已删除（`git status` 无残留）。

---

## 发现

1. **前序实现删了脚本但没删对脚本的引用** —— 这是最容易漏的一类缺口：目录级删除是显式的、可 grep 的，而**正文里散落的 `scripts/xxx.py` 命令与 `features.py` 概念引用**不会被「删目录」这个动作带走。仓库自带的 `tests/test_skill_sizes.py::test_no_bot_unusable_instructions` 只查 `execute_code` / `python scripts` / `脚本加载` **三个串**，因此**漏过了 `scripts/` 路径与裸 `.py` 引用**，测试全绿但技能仍指向不存在的脚本。任务给的验证项 3（grep `scripts/`、`execute_code`、`.py`）比仓库测试严格得多，是有效的独立判据。
2. **「提取知识」容易变成「提取名字」** —— `signal-intent-matrix.md` 与 `sequence-vocabulary.md` 的**条目数都对（63 条）**，但承载知识的列（方向/风格/信号K/入场K/判据注记）全空或丢失。**只数条目会误判为达标**；必须核对「原表的字段是否都在」。
3. **拆分会制造悬空引用** —— 4 个文件拆成 12 片后，其余文件里 21 处指向旧名的路径全部失效（`plan-schema.md`、`pattern-catalog.md`），且 11 个知识模块还指向已删的 422 KB 场记指南 HTML。拆分必须配套全库路径重写。
4. **W04 的口径比题述更严** —— `omnialpha/skillkit/validate.py:121-124` 的判据是 `len(body.split()) + CJK 字符数 > 5000`，中文按**字**计。所以「正文 ≤13,000 字符」**并不足以**消除 W04：纯中文正文到 ~5,000 字就会触发。当前正文 7,055 字符 / ~2,375 words，两项都过。
5. **`references/integration.md` 被删（偏离 T12 显式清单）** —— 原技能 `references/` 有 11 份，现为 18 份（含 4 份新增 + 10 份分片）。差额里 `integration.md`（1,445 字符，「节点1/3/4/5 契约 + 双档数据源」）**不在 T12 的删除清单里**却被删了。判定为**可接受**：它整篇是另一套四节点系统的契约，[S6] 明确「pa-analysis 的节点1/3/4 契约：不引入」，且 T117.2 侦察报告的「建议目标结构」里也没有它。**此处是主动取舍，不是遗漏**（见 §「结论 · 取舍」）。

---

## 结论

**T10 / T11 / T12 的验收标准全部满足**，三项强制验证全绿。本次在原实现基础上补齐了 3 类实质缺口（知识丢失 ×2、脚本引用残留 ×83）+ 21 处悬空路径，并把 3 份仍以「报告/脚本」为中心的 references 改写为 bot 原生。

### 文件增 / 删 / 拆计数（相对原始 101 文件 → 现 87 文件）

| 动作 | 数量 | 明细 |
|---|---|---|
| **增** | **4** | `sequence-vocabulary.md`、`signal-intent-matrix.md`、`contract-enums.md`、`delivery-gates.md` |
| **删** | **25 文件 + 3 目录** | `scripts/`（15 py，550 KB）、`agents/openai.yaml`、`outputs/.gitkeep`、`assets/guides/`（2 HTML，483,769 字符）、`assets/templates/`（3）、`report-spec.md`、`annotation-spec.md`、`template-coverage.md`、`integration.md` |
| **拆** | **4 文件 → 12 片** | `plan-schema.md`→6、`features-spec.md`→2、`pattern-catalog.md`→2、`62_bar_counting_pullback.md`→2 |
| **保留** | 67 模块 | `assets/knowledge/` 全保留（68 文件，62 号拆 2 片） |
| **本次改** | 29 文件 | +391 / −412 行（详见 §已完成 2） |

### 验证证据（实际命令输出）

**① `omnialpha skill validate` —— 0 errors 且 W03/W04 消失**

```powershell
> .venv\Scripts\python.exe -m omnialpha skill validate "skills-src/pa-analysis"
PASS  errors=0 warnings=0
EXIT=0
```

> 注：`skillkit/validate.py` 中 W03 = description 无 WHEN 子句、W04 = 正文词数 >5000；二者均不再出现（基线里 description 已含 `Use when`，正文 ~2,375 words）。

**② 尺寸守卫（`scripts/_szcheck_pa.py`，遍历技能目录，用后已删）**

```
SKILL.md body   : 7055 chars (limit 13000)
body words      : ~2375 (W04 threshold 5000)
description     : 149 chars (limit 1024), WHEN cue=['use when']
files scanned   : 87
    11370  references/plan-schema_part6.md
    11357  references/plan-schema_part4.md
    10837  assets/knowledge/32_signal_context_matrix.md
    10770  assets/knowledge/62_bar_counting_pullback_part1.md
     9798  references/plan-schema_part1.md
RESULT: PASS
EXIT=0
```

**③ 禁用引用 grep（`scripts/`、`execute_code`、`.py`）**

```powershell
> Get-ChildItem -Recurse -File -Path "skills-src\pa-analysis" |
    Select-String -Pattern 'scripts/','execute_code','\.py\b'
NO MATCHES (0 hits for scripts/ | execute_code | .py)
```

**④ 回归测试**

```powershell
> .venv\Scripts\python.exe -m unittest tests.test_skill_sizes tests.test_skillkit* 
Ran 131 tests in 11.145s
OK (skipped=1)
```

（含 `test_skill_body_within_limit`、`test_every_file_within_ref_limit`、`test_no_dead_links`、`test_no_bot_unusable_instructions`、`test_description_within_catalog_clip` 全绿。）

### 取舍 / 未做到

1. **`references/integration.md` 维持删除状态**（§发现 5）。理由：[S6] 排除节点1/3/4 契约 + 侦察报告的目标结构不含它。**如需严格照 T12 清单保留，可一行恢复**（原文在 `_incoming/pa-analysis/references/integration.md`）。
2. **未做「改造」类文件的全面语义重写**：`trade-lifecycle.md`、`pattern-catalog_part1/2.md`、`features-spec_part2.md`、`plan-schema_part1..6.md` 只做了**脚本/报告引用净化 + 悬空路径修正**，正文的「节点3 契约」叙述仍在（改成了「执行侧」）。彻底重写这些（去四节点语境）会显著扩大改动面，超出 T10–T12 的验收范围。
3. **未跑 T14 端到端**（brooks-btc `analyze_once` 验证 journal 无截断、chip 含新字段）：依赖真实 LLM 调用与 API 凭据，不在 T10–T12 范围内（T14 是独立任务，`scripts/_e2e_skill.py` 已由另一代理暂存但未跟踪）。
4. **提交由并发代理完成**：本次 29 个文件的修改完成后，**另一个并发代理在收尾提交 `b9951aa refactor(skill): 收尾清理 —— 合并 SOUL、修残留死引用、补路由表` 中一并提交**（`git show --stat b9951aa -- skills-src/pa-analysis` 显示正是这 29 文件、+391/−412）。我本人未执行 commit（提交动作未获授权）。三项验证均在 `b9951aa` 之后复跑，验证的是**已提交状态**。
5. **工作区是并发共享的**：同一 worktree 上还有代理在做 `price-action-trading`（T6–T9）与 A/B 对比，`skills-src/price-action-trading/**` 与 `scripts/_ab_old*/**`、`scripts/_e2e_skill.py` 的改动**不是本次产物**，未触碰。
5. **`SKILL.md` 的 7 个字段标为「必填」**，而 [S2] 定义它们是 **optional**。技能层面强制模型每轮都写这 7 个字段，比 schema 更严；这是前序实现的取舍（有利于 journal/复盘），我保留未改，特此记录。

### 验证复跑记录（`b9951aa` 之后）

| 验证 | 命令 | 结果 |
|---|---|---|
| ① | `python -m omnialpha skill validate "skills-src/pa-analysis"` | `PASS  errors=0 warnings=0`，exit 0 |
| ② | `python scripts/_szcheck_pa.py "skills-src/pa-analysis"`（临时脚本，已删） | `RESULT: PASS`，exit 0 |
| ③ | `Get-ChildItem -Recurse -File skills-src\pa-analysis \| Select-String 'scripts/','execute_code','\.py\b'` | `NO MATCHES`（0 命中） |
| ④ | `python -m unittest tests.test_skill_sizes` | `Ran 5 tests ... OK` |
| ⑤ | `python -m unittest tests.test_skillkit*`（8 个模块） | `Ran 131 tests in 11.145s OK (skipped=1)` |
