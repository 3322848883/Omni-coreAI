# Research Brief — 交易系统设计升级调研

**日期**：2026-10-05
**深度模式**：deep（7 个 angle，2 轮追补，目标 25+ 来源）

## 精炼问题

为一个已上线的 LLM 交易系统（`omnialpha`：Gate.io 永续合约 + 多个人格讨论组 + Python）
寻找**可移植的工程设计与架构实践**，产出一份升级设计方案。系统的现状与痛点：

- 单进程多 bot，`executor.py` 负责下单/风控，`watcher.py` 消费信号，`strategist/` 负责 LLM 决策
- 已有 3 人格讨论组（weighted_vote + 3 轮辩论），走 `persona-run` 常驻
- 已实盘运行（账户 85 USDT，50x 杠杆）
- **当前痛点**：保护单（TP/SL）与持仓张数脱钩、只增不减，6 小时堆到 202 张 vs 4 张持仓
- 已对照参考项目 `nofx`（Go，VergeX 信号驱动交易），提炼出「程序强制 + 提示词镜像」等原则

## 范围

**In**：
- 其他开源/学术 LLM 交易系统的**架构与工程设计**（不是策略收益）
- 订单/持仓/保护单的**状态一致性**工程实践
- LLM 结构化输出的**可靠性工程**（schema、校验、降级、重试）
- **风控与 kill switch** 的工程实现
- **多 agent 协作/辩论**的机制设计与评估结论
- LLM agent 在金融场景的**已知失败模式**

**Out**：
- 具体策略的盈利性、择时信号质量
- 加密货币市场预测
- 交易心理学 / 手动交易方法
- 与设计无关的营销材料

## 假设（用户未明说但据此执行）

1. 目标是**工程设计**，不是策略 alpha
2. 可移植性优先：找「能落到 Python 单体项目」的做法，而非需要重架构的方案
3. 系统规模小（单人维护、单账户、数十个 bot），不追求企业级复杂度
4. 已经读过 `nofx`，本次重点找**它没覆盖或做得不同的**思路

## Angles

| # | Angle | 为什么值得查 |
|---|---|---|
| A1 | 多 agent LLM 交易框架的架构设计（TradingAgents / FinCon / FinMem / FinAgent / CryptoAgent 等） | 直接对照我们的讨论组设计 |
| A2 | 成熟开源交易机器人的工程实践（Freqtrade / Hummingbot / Jesse / OctoBot） | 久经实盘考验的订单与风控工程 |
| A3 | LLM 结构化输出的可靠性与 guardrails 工程（schema 校验、重试、降级、function calling） | 对应我们的 Plan JSON 解析 |
| A4 | 交易所订单/持仓状态一致性：幂等、对账、孤儿单、reconciliation 模式 | 直接命中保护单堆积问题 |
| A5 | LLM 交易 agent 的实战失败模式与教训（论文/工程博客/issue） | 避免重复别人踩过的坑 |
| A6 | 风控 / 仓位管理 / kill switch 的工程最佳实践 | 我们刚上实盘，风控是短板 |
| A7 | 多 agent 辩论 / 共识机制的设计与评估（debate、consensus、voting、arbiter） | 我们做了 sync/relay 两种模式，想找外部依据 |

## 工具约束（给 sub-agent）

本机**没有 WebSearch 工具**，只有 `webfetch`（单 URL 抓取）。搜索方式：
用 `webfetch` 抓 Bing/Google/DuckDuckGo 的结果页 URL（如
`https://www.bing.com/search?q=<urlencoded>`），从返回的 markdown 里读结果链接，
再逐个 `webfetch` 打开。也可直接抓已知 URL（GitHub 仓库页、arXiv abs 页、官方文档）。

## 交付物

`research/trading-system-design-upgrade/REPORT.md` —— 升级设计方案，按「可移植性 × 收益」
排序，每条给出：外部依据（带引用）、我们的现状、具体改法、风险。
