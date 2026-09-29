# Research Brief: Agent Memory Architectures for Trading Bots

## Refined Question
各大厂商（OpenAI/Anthropic/Google/Microsoft）与开源项目（MemGPT/Letta、LangGraph、Zep、Mem0、Hermes、OpenClaw 等）的 AI agent 记忆方案是什么？如何为长时间运行的加密货币交易机器人设计专属记忆架构——覆盖订单生命周期记忆、策略决策追溯、多 agent 共享记忆、成本可控、以及未来扩展（多策略/多账户/组合管理）？

## Scope
- **In**: 2024–2026 年的 agent memory 架构、框架、论文；交易/金融 AI 的状态管理实践；上下文工程（context engineering）最佳实践
- **Out**: 传统量化回测数据库（非 AI 记忆）；非 agent 的 RAG 知识库；聊天机器人的用户偏好记忆

## Assumptions
- 受众：项目拥有者（懂交易 + 懂工程），要可落地的架构设计
- 时间框架：2024-01 至 2026-09
- 决策：选型 + 自研架构设计
- 运行环境：7×24 常驻 Python 进程，LLM 为 DeepSeek-flash（缓存命中 1/50 价），每 15 分钟一轮决策
- 已有约束：上下文恒定 ~4k token/轮，不做 dreaming/向量 RAG（初步判定）

## Depth Mode
deep（5-8 sub-agents, 2 follow-up rounds, 25+ sources）

## Angles

1. **厂商记忆架构** — OpenAI ChatGPT Memory、Anthropic Claude memory/tool-use、Google Gemini memory、Microsoft Copilot memory 的架构设计、存储模型、上下文注入策略
2. **开源 agent memory 框架** — MemGPT/Letta、LangGraph memory、Zep/Graphiti、Mem0、Cognee、Memobase 等的架构对比：存储层、检索层、上下文组装、持久化
3. **用户点名项目** — Hermes、OpenClaw 及其他 agent 记忆相关项目（含 Letta dreaming、Generative Agents 记忆流等）的具体实现
4. **学术前沿** — 2024–2026 agent memory 论文（arXiv）：记忆分型、压缩、遗忘、评测基准
5. **交易系统专用** — 金融/交易 AI agent 的状态管理、持仓记忆、策略决策日志、事件溯源（event sourcing）在交易系统的应用
6. **上下文工程** — 长时运行 agent 的上下文组装、缓存优化、前缀稳定性、token 预算管理的最佳实践
7. **失败案例与反模式** — agent memory 的常见失败模式：幻觉记忆、上下文污染、成本失控、过拟合
8. **未来扩展需求** — 多 agent 协作记忆、组合级记忆、跨策略知识迁移、记忆审计与合规
