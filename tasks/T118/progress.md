# T118 — price-action-trading bot 技能化（T6/T7/T8/T9 收尾与验证）

> 工作区：`omnialpha/.worktrees/pa-skills-upgrade`（分支 `feat/pa-skills-upgrade`）
> 落点：`skills-src/price-action-trading/`（git 跟踪的源；`skills/` 是 gitignore 的安装产物，未动）
> 本轮未提交（改动留在工作树，由父代理决定是否 commit）

## 目标

把 `price-action-trading` 从「面向有代码执行能力的人类/IDE」改造为 omnialpha 的 bot 技能，交付
`docs/compose/spec/pa-skills-upgrade.md` 的 **[S3] T6–T9**：

- **T6** 重写 `SKILL.md`（≤13,000 字符、`description` ≤200 字符、含 8 个设计段落）
- **T7** `theme1`–`theme12` 拆成 ≤12,000 字符分片；新增 `00-core-steps.md` / `06-rules-index.md` / `07-pitfalls-bans.md`；`SOUL.md` 压到 ≤12,000
- **T8** 删 C 类内容（scripts/data/logs/templates/19 个 references/乱码 locale），把 27 篇原文语料移到 `docs/pa-source/`
- **T9** 修步数与版本漂移、删三个不存在的目录引用（`memory/`、`logs/analyses/`、`logs/orders/`）

## 已完成

### 0. 入场时的既有状态（重要）

进入本轮时，`git log` 已存在 `32fc1d8 feat(skill): price-action-trading 改造为 bot 技能（T6-T9）`，
即 **T6–T9 的主体已被上一轮实现并提交**（`06d2947..HEAD` 对该技能：178 files changed、+25,251 / −66,326）。
本轮做的是**按验收标准逐项核对 + 补齐未达成的部分**，不是从零重写。

### 1. 补齐 T9（漂移）—— 本轮主要工作

- 清掉三个不存在的目录引用：`memory/`、`logs/analyses/`、`logs/orders/`（原残留于 13 个文件），
  改写为 bot 的承载物（订单上下文 / 决策日志 / chip 字段）。同时顺带清掉同源死引用
  `assets/templates/*`、`knowledge/source/*`、`account.db`、裸 `market_state.md` / `order_log.md` /
  `trading_journal.md` / `trade_review.md`、裸 `logs/`。
- 版本号统一到 **v35.0**：清掉 `v33`/`v32`/`v30`/`v25`/`v24`/`v23`/`v22`/`v20`/`v18`/`v15`/`v8.0`/`v2.1`/`v1.2`
  共 13 种标注（23+3 个文件）。历史内容标注改为中性描述（如「（从v8.0恢复）」→「（历史附录）」）。
- 步数口径统一为 **26 主干 + 53 细粒度**（`SKILL.md` 与 `00-core-steps.md` 明写这两个数；
  剩余 `\d+步` 命中全是「4 步」「5 步心理准备」等局部小流程，不是步数口径）。

### 2. 补齐 T7

- **`references/SOUL.md`**：原 17,036 字符被切成 `SOUL_part1/2` 两片（内容未压）。本轮按方案合并回**单份
  `SOUL.md`（4,815 字符）**，删掉路径解析 + Python 代码块、长 ASCII 流水线图、三场景举例、
  记忆系统/模板/原文来源表、写入文件规则表、状态管理者职责；保留身份、核心文件、8 阶段概览、
  核心原则、执行规范（26 步/SB-CT/Step 4.0/BAN/决策自检/全自动/数据加载/交易成本）、行为准则。
  删除 `SOUL_part1.md`、`SOUL_part2.md`。
- **`references/00-core-steps.md`**：原文件被截断（末尾悬空 ```）+ 内容重复。重建为 4,673 字符：
  26 主干步骤定义 + `Step → workflow × strategy_workflow` 映射表 + 主干步骤速查（判据/不通过动作）
  + HC-01~10 摘要。
- **`references/07-pitfalls-bans.md`**：重写为 4,185 字符，去掉重复标题、死文件引用、env 变量路径、
  `memory/`/`templates` 引用、版本标注；保留 Pitfall 1–16 + 附录D 禁止事项（P0/P1/P2）+ 出手前速查。
- **`assets/examples/01-btc-20260602.md`**：5,724 → **2,905 字符**（保留 26 步判据与结论密度，去掉表格冗余），
  并补上「对应的 chip 字段」示例。
- `references/06-rules-index.md` 修 3 处：`（v33 新增）`、`account-watcher` 来源说明、`memory/*.md` 写入步骤。

### 3. 补齐 T8

- 删除 `references/analysis-execution-guide.md`（5,027 字符；不在保留清单内，且整篇是
  `memory/` 路径 + 已删除文件引用 + 「49 个细粒度步骤」漂移，属 C 类被 `SKILL.md` 路由表 + `00-core-steps.md` 取代）。
- 复核既有删除：`scripts/`(2)、`data/`、`logs/`、`assets/templates/`(14)、`assets/examples/` 除 01 外 4 份、
  `locales/zh-CN.json`、`references/` 下 19 个 C 类文件、`references/history/`(6) 全部不存在；
  `references/knowledge/source/` 不存在，`docs/pa-source/` 有 **27** 篇。

### 4. 补齐 T6

- `SKILL.md` 路由表补上 `references/SOUL.md`、`mindmap-gap-list.md`、止损整合/加密止损框架、格式范例；
  把 `workflow.md`/`strategy_workflow.md` 改成实际存在的分片名（`workflow_*.md`/`strategy_workflow_*.md`），
  并注明原文语料已移到仓库 `docs/pa-source/`。

### 5. 最终形态

技能目录 **98 个文件**：`SKILL.md` + `locales/en-US.json` + `assets/examples/01-btc-20260602.md`
+ `references/` 7 份（`SOUL.md`、`00-core-steps.md`、`06-rules-index.md`、`07-pitfalls-bans.md`、
`mindmap-gap-list.md`、`stop-loss-rules-consolidated.md`、`crypto-stop-loss-framework.md`）
+ `references/knowledge/` 88 份（theme1–12 共 56 片、`strategy_workflow` 6 片、`workflow` 19 片、
`instrument_crypto_specifics` 2 片、theme13–17 原样 5 份）。

## 发现

1. **T6–T9 在上一轮已提交**（`32fc1d8`），本轮实际是**验收驱动收尾**：规格里的 T9 只被做了一半
   ——上一轮给受影响的文件加了「本文件源自人类工作流…」的免责 banner，但 `memory/`、`logs/analyses/`、
   `logs/orders/` 这些 token 仍原样留在正文里（13 个文件），`assets/examples/01-btc` 也未压到 ≤3,000。
2. **`worktree` 内存在并发写入**：本轮执行期间 `skills-src/pa-analysis/**` 被另一进程持续修改
   （如 `features-spec_part1.md` 于 21:01:56 变更），说明有并行子代理在同一 worktree 工作。
   本报告的所有结论**只对 `skills-src/price-action-trading/` 有效**；`git status` 的其它条目不属于本轮。
3. **尺寸口径确认**：`skillkit/tool.py:106` 用 `read_text(encoding="utf-8", errors="replace")` 读文件，
   通用换行 → 计数是 **LF 口径**。同一文件 PowerShell `ReadAllText` 会多出 `\r` 数
   （例：`06-rules-index.md` = 12,178（CRLF 原始）vs **11,792**（LF 生效值））。判断必须用 LF 口径，
   否则会把安全文件误判为超限。
4. **`references/06-rules-index.md` 是 11,792 字符**，离 12,000 上限只剩 208 字符 —— 后续任何人往这份文件加内容都会越界。
5. 原 `00-core-steps.md` 是**写坏的**：末尾悬空代码块 + 「26步全部执行」段落重复两次。已被重建。
6. `tests/test_skill_sizes.py` 已把尺寸约束钉住（`SKILL.md`≤13,000、每份 references≤12,000、
   `description`≤200 且须含 "Use when"、无死链、无 `execute_code`/`python scripts`/`脚本加载`）。
   它是本轮最有价值的护栏 —— 但**不检查** `memory/`、`logs/analyses/`、`logs/orders/` 这类死目录引用，
   也不检查版本号数量，所以 T9 只能靠人工核对。

## 结论

**T6/T7/T8/T9 现已全部达到验收标准**（本轮新增 4 个文件、重写 5 份、删除 3 份、批量清理 34 份；
本轮工作树 delta：39 files changed，+368 / −1,253）。

### 验证证据（实际命令输出）

**① `skill validate`（必须 0 errors，W04 body 超 5000 words 也要消除）**

```
$ "C:\Users\w6485\Desktop\测试\omnialpha\.venv\Scripts\python.exe" -m omnialpha skill validate "skills-src/price-action-trading"
PASS  errors=0 warnings=0
```

（`warnings=0` 即 W04 已消除 —— 正文 9,143 字符、中文按字计也不超 5,000 words。）

**② 尺寸守卫（临时脚本 `scripts/_szcheck.py`，用后已删）**

```
SKILL.md body = 9143  (limit 13000)
description   = 155   (limit 200)

-- 文件清单（98 个，按字符数降序，只列 >=6000 的）--
 11792  references\06-rules-index.md
 11521  references\knowledge\theme3_pullbacks_part4.md
 11508  references\knowledge\strategy_workflow_part2.md
 11500  references\knowledge\workflow_part8.md
 ...（其余 93 份全部 < 11,500）
最小值 103 / 最大值 11792

-- 结论 --
OK: SKILL.md<=13000, 所有 references<=12000, description<=200
```

关键交付物尺寸（LF 口径）：`SOUL.md` 4,815｜`00-core-steps.md` 4,673｜`06-rules-index.md` 11,792｜
`07-pitfalls-bans.md` 4,185｜`assets/examples/01-btc-20260602.md` 2,905｜`SKILL.md` 正文 9,143。

**③ 无 bot 不可用指令**

```
$ python -c "... pats=['execute_code','python scripts','scripts/','bash','memory/','logs/analyses/','logs/orders/'] ..."
hits = []
```

**④ 回归（尺寸守卫 + skillkit 测试）**

```
$ python -m unittest tests.test_skill_sizes tests.test_skillkit
Ran 30 tests in 0.401s
OK
```

**⑤ T8 删除/搬迁复核**

```
19 个 C 类 references 文件：all 19 gone
references/knowledge/source 存在：False
locales/zh-CN.json 存在：False
scripts/ False | data/ False | logs/ False | assets/templates/ False
docs/pa-source/ 文件数：27
```

### 取舍与未做到

- **未提交**：本轮改动留在工作树（任务未授权 commit）。
- **多删了 1 个文件**：`references/analysis-execution-guide.md`（不在 T8 的 19 项清单里，但内容 100%
  是 `memory/` 路径 + 已删文件引用 + 49 步漂移，保留即违反 T9；已在上面说明）。
- **`references/history/`（6 份）** 沿用上一轮的删除（T8 清单未列，但 T117.1 侦察的删除清单含它，
  且 6 份里 3 份 >12,000 字符，保留即违反尺寸硬约束）。
- **`theme13–17` 未动**（按要求）；`strategy_workflow.md` 与 `instrument_crypto_specifics.md`
  原文件 >12,000，上一轮已分片（6 片 / 2 片），本轮只做漂移清理。
- **死引用清理做了延伸**：除 T9 点名的三个目录，还清了 `assets/templates/*`、`knowledge/source/*`、
  `account.db` 等**因本轮删除而产生的**死引用 —— 否则「读得到但指向不存在的东西」的问题会原样残留。
- **未跑端到端**：T14 的 `brooks-btc analyze_once` 实跑不在 T6–T9 范围内，未执行。
- **并发风险**：同一 worktree 有并行子代理在改 `skills-src/pa-analysis/**`，本轮未触碰该目录。
