# 如何添加一个 Skill（SkillKit）

skill 是可安装、按需加载的能力包（SKILL.md + 可选 references/scripts/assets）。
**只影响 LLM 的分析方法论与观点，不改变下单动作与风控约束。**

## 1. 目录契约

```
skills/<skill-id>/
├── SKILL.md          # 必填
├── references/       # 可选，按需文档（模型自己读）
├── scripts/          # 可选，本期不执行
└── assets/           # 可选，模板/查找表
```

`<skill-id>` 必须 kebab-case（小写字母数字连字符），并与 SKILL.md 里 `name` 一致。

## 2. SKILL.md frontmatter

```yaml
---
name: my-strategy
description: 做什么 + 何时用（必须含触发句）+ 不要用于什么。Use when 用户提到 …。Do not use for …。
version: 1.0.0
license: MIT
compatibility: 需要 Python 3.10+
---
```

硬规则（校验器强制）：
- `name` / `description` 必填；description ≤1024 字符
- **frontmatter 禁 XML 尖括号 `<` `>`**
- description 必须含 WHEN 触发句（`Use when` / `使用场景` / `当用户`）
- skill 目录内不要放 README.md
- 引用文件用相对路径且必须在 skill 目录内

可选：`model-invocation: false`（用户/CLI 专属，模型不可自主触发）、`allowed-tools: "klines indicators"`（激活时收窄工具面）。

## 3. 安装与启用

```powershell
# 校验（fail-closed，有 ERROR 拒绝）
python -m gate_bot skill validate path/to/my-strategy

# 安装到 <root>/skills/（不会自动启用）
python -m gate_bot skill install path/to/my-strategy

# 查看
python -m gate_bot skill list
python -m gate_bot skill show my-strategy

# 启用：编辑 config/bots/<bot>.yaml
#   skills: [my-strategy]
# 空列表 skills: [] = 该 bot 无 skill

# 卸载
python -m gate_bot skill remove my-strategy --yes
```

## 4. 运行时行为

- **L1 catalog**：plan-loop 启动时把 `name + description` 注入 system prompt（`<skill_catalog>`），body 不预载
- **L2 加载**：模型调用 `skill(name)` 工具 → body 以 `[skill:id]…[/skill:id]` 注入本回合
- **L3**：body 中引用的 `references/xxx.md` 由模型自己读取
- 每次激活写 `logs/skill_journal.jsonl`
- 预算：catalog 默认 ≤2000 token；单 skill body 建议 <5000 token

## 5. 红线

1. skill 只能影响 Plan JSON 的观点/理由；executor + yaml 风控是唯一执行闸门
2. skill 目录永不放密钥
3. 有副作用的 skill 必须 `model-invocation: false`
4. 坏包 fail-closed：校验不过不安装

## 6. 测试

```powershell
python -m unittest tests.test_skillkit
```
