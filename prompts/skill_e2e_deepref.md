# 任务：深挖价格行为规则原文
你是量化策略引擎。用户请求：**我需要 H2 和 L2 的完整定义、触发条件、失效条件，以及 Brooks 原文出处。请查阅技能的知识库给出完整规则，不要泛泛而谈。**

## 要求
1. 先 `skill(price-action-trading)` 加载技能。
2. 技能正文指向 `references/` 下的深层文档 → **用 `skill_ref(name, path)` 读取**（例如 references/SOUL.md、references/knowledge/strategy_workflow.md、references/knowledge/theme3_pullbacks.md）。
3. 基于读到的原文，给出 H2/L2 的完整规则（含编号引用）。
4. 最后输出 Plan JSON（hold 即可）。

允许动作：hold。
