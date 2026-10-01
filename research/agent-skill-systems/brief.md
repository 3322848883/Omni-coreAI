# Research brief — AI 智能体 Skill / 插件 / MCP 体系设计调研

## Refined question

各大厂商与开源项目（Anthropic、OpenAI、MCP 生态、OpenClaw、Hermes、LangChain/LangGraph、AutoGPT、CrewAI 等）如何设计 **AI 智能体 Skill / 插件 / 工具扩展体系**？其架构约定、加载与调用机制、可靠性与稳定性保障（沙箱、权限、版本、故障隔离、评测）是什么？这些模式如何映射到 **gate-signal-bot**（Gate 永续合约信号执行 + LLM 策略 monorepo）——一个已有 20 个原生 function-calling 工具、prompts/ 人格体系、多人格共管、25 族指标的量化交易智能体——以支持可安装、可组合的 **skill**，向「AI 交易全方位智能体」（tools / plugins / skills / MCP）演进？

## Scope

**In:**
- Skill / plugin / extension 的**格式约定**（SKILL.md、manifest、目录结构、元数据）
- **加载与发现机制**（何时注入、渐进式披露、按需调用、上下文预算）
- **调用模型**（纯指令注入 vs 附带工具 vs 子代理 vs MCP server）
- **可靠性/稳定性**：沙箱执行、权限与确认、版本化、依赖管理、故障隔离、超时、幂等、审计日志
- **组合与安装**：skill 之间组合、依赖、冲突处理、市场/目录分发
- 交易/量化场景的 skill 化实践（策略人格、分析流程、风控规则打包）
- 面向 2025–2026 的最新设计（近 12 个月优先）

**Out:**
- 通用 LLM 对齐/训练方法
- 具体交易信号的 alpha 研究
- 纯 UI 插件（浏览器扩展、编辑器插件）除非其架构对 agent skill 有直接借鉴

## Assumptions
- 受众：gate-signal-bot 的开发者/架构师，要落地实现而非纯学术
- 决策：选定 skill 格式、加载协议、与现有 tools/prompts 的集成方式、可靠性红线
- 时间框：2025-01 ~ 2026-09，兼顾仍在服役的经典设计
- "OpenClaw"、"Hermes" 按用户提法检索，若对应多个项目则都覆盖并注明

## Depth mode
**deep**（5-8 子代理，2 轮跟进，目标 25+ 来源）

## Angles

1. **Anthropic Agent Skills / Claude Code**：SKILL.md 格式、渐进式披露、skills 目录约定、触发与调用、与 subagents/MCP 的边界
2. **MCP（Model Context Protocol）**：官方 spec 架构、server 设计模式、资源/工具/提示原语、可靠性与安全模型、版本演进
3. **OpenAI 与主流闭源厂商的工具/插件体系**：function calling 稳定性实践、GPTs/Actions、插件/扩展机制、可移植的 skill 等价物
4. **开源 agent 框架的扩展体系**：LangChain/LangGraph、AutoGPT plugins、CrewAI、AutoGen、smolagents、OpenClaw、Hermes 等的 skill/plugin/tool 注册与组合
5. **量化/交易智能体的策略打包实践**：TradingAgents、FinGPT、AI Hedge Fund 等开源交易 agent 如何封装策略、指标、风控为可复用模块
6. **可靠性与安全模式**：工具/skill 沙箱、权限确认、版本锁定、故障隔离、幂等、评测（evals）、审计与回放
7. **格式对比与选型**：SKILL.md vs MCP tool vs plugin vs prompt-template vs subagent 的适用边界、上下文成本、组合性对比

## Notes for orchestrator
- 用户明确要求报告要「方便我们做这个功能」，结论要能落到 gate-signal-bot 的设计决策
- 工作区：research/agent-skill-systems/
