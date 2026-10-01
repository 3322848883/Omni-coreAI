---
feature: skillkit
status: delivered
updated: 2026-10-01
branch: master
design_ref: research/agent-skill-systems/REPORT.md
---

# SkillKit — 可安装 Skill 引擎（skill 工具功能）

## Report

**What was built** — `gate_bot/skillkit/`：可安装 skill 引擎，三级渐进披露。
- **L1** catalog：启动只把 `name + description`（含使用提示）注入 system prompt；body 永不预载
- **L2** `skill(name)` 工具：按需载入 SKILL.md 正文
- **L3** `skill_ref(name, path)` 工具：按需读 `references/`/`scripts/`/`assets/`（realpath 遏制）
- 模块：`models / loader（Safe-YAML）/ validate（E01-E10, W01-W05）/ registry / budget / catalog / tool / journal / cli`
- 集成：`tools.py`（22 工具）、`prompt.py`（catalog 插槽）、`loop.py`（catalog 注入 + 白名单 + allowed-tools 收窄）、`__main__.py`（`skill` 子命令）
- 安装：`python -m gate_bot skill install <dir>`（fail-closed，不自动启用）；启用 bot yaml `skills: [id]`

**Verification** — 七轮生产测试 + 真实行情全链路：
- skillkit 单测 **125 PASS**；全量 **859 PASS**
- **22 工具真实行情全通**（Gate testnet，0 失败）
- 真实 LLM 端到端：skill 激活 → 多维工具分析 → Plan（A/B 对比确认方法论注入有效）
- 服务器生产测试（见 §Report 尾）

**Journey log** — 七轮端到端测试暴露并修复 **8 个 bug**（详见 §Bug 修复记录）：
1. catalog 缺使用提示 → LLM 不知调 skill
2. **白名单形同虚设**（`skills:[]` 仍能加载，权限绕过）
3. **L3 不可达**（工具面无文件读取 → 新增 `skill_ref`）
4. **allowed-tools 未执行**（声明无效果 → 运行时收窄）
5. 空 frontmatter 误判未闭合
6. **并发 journal 丢写**（跨进程丢 13% → OS 文件锁）
7. 坏 UTF-8 崩 scan
8. 单次 `plan` 遇 LLM 错误抛裸 traceback

## Bug 修复记录

| # | 问题 | 严重度 | 修复 |
|---|------|:---:|------|
| 1 | catalog 缺使用提示 | 中 | `render_catalog` 注入「先调 skill(name)」 |
| 2 | 白名单形同虚设 | **高** | `run_tool(bot_id, skill_ids)` 由 runner 注入，不信 LLM 传参 |
| 3 | L3 不可达 | 中 | 新增 `skill_ref` 工具（realpath 遏制 + 白名单 + 截断） |
| 4 | allowed-tools 未执行 | 中 | `_chat_native_tools` 激活后收窄工具面 |
| 5 | 空 frontmatter 误判 | 低 | 解析器加空块特判 |
| 6 | 并发 journal 丢写 | **高** | threading.Lock + OS 文件锁（msvcrt/flock） |
| 7 | 坏 UTF-8 崩 scan | 中 | 编码错误转 SkillError + scan 兜底 |
| 8 | 单次 plan 裸 traceback | 中 | `run_once` 捕获 LLMError → 优雅降级 |


## [S1] Problem

gate-signal-bot 的 LLM 策略层目前只有两层扩展面：**20 个 function-calling 工具**（确定性数据/动作接口）与 **prompts/ 人格文件**（一次性整包注入）。缺中间层——**可安装、可组合、按需加载的 skill**：

- 策略方法论（Brooks 26 步、SMC 框架、风控 checklist）现在要么塞进 prompt 常驻（token 爆炸），要么写死在人格文件里（不可组合、不可单独升级）
- 没有「安装/校验/启停」生命周期：第三方或自研能力包无法安全落地
- 没有 catalog + 按需加载机制：模型不知道「有什么能力可用」，也无法只在匹配时才付 token 成本

**目标**：实现 skill **引擎**（发现 → 校验 → catalog 注入 → 按需加载 → 启停治理），不是某个 skill 内容。skill 内容是后续填充物。

**非目标（本期不做）**：skill 内脚本执行、市场分发/SHA-256 信任封、skill 直接下单、MCP server 桥接。

**红线**：skill 一律 advisory——只能影响 Plan JSON 的观点/指标选择；`executor + yaml 风控` 是唯一执行闸门，skill 无任何权限扩张。

## [S2] Design

### [S2.1] 架构总览

```
<root>/skills/<skill-id>/          ← 安装根（全局）
<root>/.mimocode/skills/<id>/      ← 项目级（可选，优先级更高）

        │  scan（plan-loop 启动 + 配置变更）
        ▼
┌────────────────── gate_bot/skillkit/ ──────────────────┐
│  models.py     SkillMeta / SkillPackage dataclass       │
│  loader.py     发现 + Safe-YAML frontmatter 解析        │
│  validate.py   安装期校验（fail-closed）                 │
│  registry.py   SkillRegistry：id → meta + 每 bot 可见集  │
│  budget.py     catalog token 预算 + 溢出降级             │
│  catalog.py    system prompt <skill_catalog> 渲染        │
│  tool.py       NATIVE_TOOLS 定义 + run_tool("skill")     │
│  journal.py    激活审计（logs/skill_journal.jsonl）      │
└─────────────────────────────────────────────────────────┘
        │                                │
        ▼                                ▼
 <skill_catalog>（L1，仅 name+desc）   skill(name) 工具（L2，注入 body）
        │                                │
        └────────► LLM strategist ◄──────┘
                        │
                        ▼  Plan JSON（advisory）
              executor + yaml 风控（不变）
```

### [S2.2] skill 目录契约

```
skills/<skill-id>/
├── SKILL.md          # 必填
├── references/       # 可选，按需文档（模型自己 Read）
├── scripts/          # 可选，本期只允许被引用不允许执行
└── assets/           # 可选，模板/查找表
```

`SKILL.md` frontmatter（loader 白名单，多余键丢弃并 WARNING）：

| 字段 | 必填 | 规则 | 引擎行为 |
|------|------|------|----------|
| `name` | ✅ | kebab-case `^[a-z0-9]+(-[a-z0-9]+)*$`；必须 = 目录名；禁 `gate`/`gbot` 等保留字 | 稳定 ID |
| `description` | ✅ | ≤1024 字符；必须含 WHEN 触发句（`Use when`/`使用场景`）；**禁 XML 尖括号** | 进 catalog |
| `version` | 可选 | 字符串 | 元信息 |
| `license` | 可选 | 字符串 | 跨包分发字段 |
| `compatibility` | 可选 | ≤500 字符 | 环境要求 |
| `allowed-tools` | 可选 | 空格分隔工具名 | **收窄**该 skill 激活时的工具面 |
| `metadata` | 可选 | string→string map | `metadata.gate.risk_level`、`metadata.gate.model_invocation` 等 |
| `model-invocation: false` | 可选 | bool | 用户/CLI 专属，模型不可自主触发 |

**解析约束（安全）**：
- Safe-YAML：只做 key:value 解析，**无代码执行**（不用 `yaml.load` 的任意对象构造）
- frontmatter 全文禁 `<` `>`（会注入 system prompt，防标签伪装注入）
- 保留字 name 禁令（防冒充系统内置）

### [S2.3] 数据模型

```python
# gate_bot/skillkit/models.py
@dataclass(frozen=True)
class SkillMeta:
    id: str                    # = name = 目录名
    description: str
    path: Path                 # skill 根目录
    version: str | None = None
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: frozenset[str] | None = None  # None = 不收窄
    model_invocation: bool = True                # False = 用户专属
    risk_level: str = "advisory"                 # metadata.gate.risk_level
    body_tokens: int = 0                         # 预算用（估算）

@dataclass
class SkillPackage:
    meta: SkillMeta
    body: str                  # SKILL.md 正文（不含 frontmatter）
    files: dict[str, Path]     # references/scripts/assets 相对路径映射
```

```python
# gate_bot/skillkit/registry.py
class SkillRegistry:
    def scan(self, roots: list[Path]) -> list[SkillMeta]: ...
    def visible_for(self, bot_id: str) -> list[SkillMeta]: ...
    def get(self, skill_id: str, bot_id: str | None = None) -> SkillPackage: ...
    def load_body(self, skill_id: str) -> str: ...   # L2 加载
```

### [S2.4] 发现与 catalog 注入（L1）

**扫描根优先级**（同名取高优先级）：
1. `<root>/.mimocode/skills/`（项目级）
2. `<root>/skills/`（全局安装根）

**catalog 渲染**（`catalog.py`）：

```xml
<skill_catalog>
- brooks-pa: Al Brooks 价格行为分析…（≤200 字符截断）
- smc-sniper: SMC 精确入场检查…
</skill_catalog>
```

- 只注入 `name + description`（description 截断到 200 字符/条）
- **body 永不预载**
- 预算（`budget.py`）：catalog 合计 ≤ 2000 token（默认）或 context 的 1%（取小）；溢出降级顺序：砍 description → 砍低频 skill → 截断列表
- 频率信号：`journal.py` 统计近 N 天激活次数，作为降级排序依据

**可见性**（bot yaml）：

```yaml
# config/bots/xxx.yaml
skills: [brooks-pa, smc-sniper]   # 非空即最终集，不与默认合并
skills: []                        # 该 bot 无 skill（不渲染 catalog）
# 缺省 skills 键 = 扫描根下全部 model_invocation=true 的 skill
```

### [S2.5] skill 工具（L2，核心交付物）

`NATIVE_TOOLS` 第 21 个：

```json
{
  "type": "function",
  "function": {
    "name": "skill",
    "description": "Load a skill's full instructions by name. Use when the task matches a skill listed in the skill_catalog. Loading is per-turn: the skill body becomes instructions for this turn only.",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {"type": "string", "description": "skill id from the catalog"}
      },
      "required": ["name"]
    }
  }
}
```

`run_tool("skill", {"name": ...})` 流程：

1. registry 查 id → 不存在 → 返回错误 + 可用 id 列表
2. `model_invocation=False` → 拒绝（提示用户用 CLI）
3. bot `skills` 列表未含 → 拒绝
4. 读 body → **作为 tool result 返回**（模型下一轮把它当指令执行）
   - tool result 里包一层约定标记：`[skill:brooks-pa]\n<body>\n[/skill:brooks-pa]`
   - body 超 `max_body_tokens`（默认 5000）→ 截断 + WARNING
5. `allowed-tools` 若设置 → 后续回合工具面收窄（工具列表过滤）
6. 写 journal：`{"ts", "bot_id", "skill_id", "body_tokens", "turn"}`

**L3**：body 中出现的 `references/xxx.md` 由模型自己用现有文件读取能力加载；引擎只在 validate 期检查引用文件存在性。

### [S2.6] 安装期校验（validate.py，fail-closed）

CLI：`python -m gate_bot skill install <src_dir> [--bot <id>]`

```
ERROR（拒装，退出码 1）
  E01 目录名非 kebab-case / 与 name 不一致
  E02 SKILL.md 缺失或大小写不精确
  E03 skill 目录内出现 README.md
  E04 frontmatter 缺失/畸形/非 Safe-YAML
  E05 frontmatter 含 < 或 >
  E06 缺 name 或 description
  E07 name 非 kebab-case 或含保留字
  E08 description > 1024 字符
  E09 compatibility > 500 字符
  E10 引用路径越界（realpath 不在 skill 目录内）

WARNING（可装，退出码 0）
  W01 description < 40 字符（触发难）
  W02 description 无 WHEN 触发句
  W03 body > 5000 词（应拆 references）
  W04 SKILL.md 引用的文件不存在
  W05 未知 frontmatter 键（已丢弃）
```

安装 = 校验 → 复制进 `skills/<id>/` → **不自动启用**（写 `skills/<id>/.installed` 证据文件）。启用仍靠 bot yaml `skills:`。

### [S2.7] CLI 面

```
python -m gate_bot skill install <path> [--bot ID]   # 校验+安装（不启用）
python -m gate_bot skill list [--bot ID]             # 已装 + 启用状态 + body_tokens
python -m gate_bot skill show <id>                   # meta + 校验摘要
python -m gate_bot skill validate <path|id>          # 只校验
python -m gate_bot skill remove <id>                 # 卸载（需 --yes）
python -m gate_bot skill run <id> --bot ID           # 用户专属 skill 的 CLI 入口
```

### [S2.8] 配置

```yaml
# config/skills.yaml（可选，全默认可跑）
skillkit:
  enabled: true
  roots: []                      # 额外扫描根，默认 [<root>/.mimocode/skills, <root>/skills]
  max_catalog_tokens: 2000
  max_body_tokens: 5000
  description_clip: 200
  model_invocation_default: true
  journal: logs/skill_journal.jsonl
```

bot yaml 新增键：`skills: [id, ...]`（见 S2.4）。

### [S2.9] 安全红线（实现时不可妥协）

1. frontmatter Safe-YAML、禁尖括号、保留字禁令
2. 引用路径 realpath 遏制（必须在 skill 目录内）
3. skill body **只影响 Plan JSON**；不注册任何新执行动作；`allowed-tools` 只能收窄
4. skill 目录永不出现密钥；脚本本期不执行
5. `model-invocation: false` 的 skill 模型不可见不可调
6. 每次激活写 journal（可审计回放）

### [S2.10] 与现有面的对接点

| 现有模块 | 改动 |
|----------|------|
| `strategist/tools.py` | `NATIVE_TOOLS` 追加 `skill` 定义；`run_tool` 分派 `skill`；`TOOL_GUIDE` 加一段 catalog 用法 |
| `strategist/loop.py` | prompt 组装时调 `catalog.render(bot_id)`；工具面按 `allowed-tools` 过滤 |
| `strategist/prompt.py` | system prompt 预留 `<skill_catalog>` 插槽 |
| `__main__.py` | 子命令 `skill`（install/list/show/validate/remove/run） |
| `config/bots/*.yaml` | 可选 `skills:` 键（schema 校验） |
| tests/ | 新增 `test_skillkit_*.py` |

**不改**：executor、risk、bridge、inbox 契约、watcher——skill 碰不到执行链。

## [S3] Tasks

| # | 任务 | 验收（acceptance） | 覆盖 |
|---|------|-------------------|------|
| T1 | `skillkit/models.py` + `loader.py`：Safe-YAML frontmatter 解析、白名单字段、SkillMeta | 合法包解析出 meta；畸形/尖括号/越界路径被拒 (covers: S2.2 S2.3 S2.9) |
| T2 | `skillkit/validate.py`：E01–E10 / W01–W05，退出码约定 | 坏包清单逐条单测；好包 PASS 0 error (covers: S2.6) |
| T3 | `skillkit/registry.py` + `budget.py`：扫描根优先级、每 bot 可见集、catalog 预算降级 | 同名取高优先级；`skills: []` 不渲染；超预算降级可测 (covers: S2.4) |
| T4 | `skillkit/catalog.py` + `tool.py`：`<skill_catalog>` 渲染、`skill` 工具定义与 run_tool 流程、journal | catalog 只含 name+desc；skill 工具载入 body 带标记；无权限/未启用拒绝 (covers: S2.5) |
| T5 | CLI `skill install/list/show/validate/remove/run` | install 不自动启用；list 显示启用态；remove 需确认 (covers: S2.7) |
| T6 | 对接：tools.py / loop.py / prompt.py / bot yaml schema | plan-loop 带 catalog 启动；skill 工具可被 mock LLM 调用 (covers: S2.10) |
| T7 | 测试包 fixture：1 个好包 + 3 个坏包（尖括号/超长/越界引用）+ 全量回归 | `unittest discover` 全绿；新增 ≥20 用例 (covers: S2.6 S2.9) |
| T8 | 文档：`docs/HOWTO-add-skill.md` + README 段落 + AGENTS.md 5b 更新 | 按文档能独立装一个 skill (covers: S2.2 S2.7) |

## [S4] Rollout

| 阶段 | 内容 | 退出门槛 |
|------|------|----------|
| **P1（本 spec）** | 引擎全套：loader/validate/registry/budget/catalog/tool/CLI/journal | T1–T8 全绿；装测试 skill：catalog 只见 name+desc、skill 工具能载 body、token 在预算内、坏包被拒 |
| **P2** | 第一个真实策略 skill（如 brooks-pa 内容包）+ 触发率评测 | 正/负触发准确；with/without token 对比数据 |
| **P3** | scripts/ 执行（超时+审计+幂等键）；`metadata.tools` 懒连接 | 数字可溯源；执行失败转模型可读错误 |
| **P4** | 打包分发：SHA-256 manifest、信任封、发布≠可用 | fail-closed 安装 |

**明确不做（P1）**：脚本执行、自动装包、skill 直接下单、body 常驻 prompt、marketplace。

## [S5] 测试矩阵

| 用例组 | 关键断言 |
|--------|----------|
| loader | 合法 frontmatter 全字段；未知键丢弃+WARN；非 YAML/尖括号/保留字 → error |
| validate | E01–E10 逐条 fixture；W01–W05 不阻断；退出码 0/1/2 |
| registry | 双根同名优先级；bot 可见集；`skills:[]` |
| budget | 2k 上限；降级顺序；body_tokens 估算 |
| catalog | 只含 name+desc；截断；无可见 skill 时不渲染 |
| tool | 正常载入；未启用拒绝；`model-invocation:false` 拒绝；journal 写入 |
| e2e | mock LLM 调 skill 工具 → body 注入 → Plan 输出含 skill 观点（advisory） |
| 回归 | 既有 701 测试全绿 |

## [S6] Open questions

1. `allowed-tools` 收窄的执行时机：工具列表过滤 vs 运行期拒绝？（建议双保险）
2. body 跨轮驻留策略：本轮只注入 tool result（一次性）还是像 Claude Code 那样跨轮驻留？（P1 先做一次性，简单且省 token）
3. 多人格共管下 skill 可见性：per-bot 还是 per-persona-group？（P1 按 bot，group 场景 P2 再看）
