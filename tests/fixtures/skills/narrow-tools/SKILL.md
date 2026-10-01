---
name: narrow-tools
description: "Narrow-tools test skill that restricts the tool surface. Use when testing allowed-tools narrowing behavior in SkillKit."
allowed-tools: "klines indicators ticker"
---

# Narrow Tools (test fixture)

本 skill 声明 `allowed-tools: klines indicators ticker`，用于验证：
- SkillMeta.allowed_tools 正确解析为 frozenset
- 激活该 skill 后工具面收窄（仅这三个）

内容仅测试用。
