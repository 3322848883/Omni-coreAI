# 案例库（examples/）— 从基础到高级

按难度排列；每个 JSON 可直接丢进 `inbox/<bot_id>/`（改 `label`/价格后）。

| 级别 | 目录 | 内容 |
|------|------|------|
| **L1 基础** | `signals/` | 单笔开仓、止盈止损、平仓、hold |
| **L2 订单类型** | `signals/` | limit / post_only / ioc / fok、trigger vs limit_order |
| **L3 突破/保护** | `signals/` | stop_entry 突破、TP-SL 组合 |
| **L4 多单网格** | `signals/06-*.json` | 单向网格 |
| **L4b 双向网格** | `signals/10-dual-grid.json` | 多空同时挂网（dual 持仓） |
| **L5 事件触发** | `triggers/` | 定时、K 收盘、EMA/ATR/RSI/突破条件 |
| **L6 多机器人** | `bots/` + `combo/` | 单订单机器人 / 网格机器人 / 三策略组合 |
| **L7 LLM Plan** | `plans/` | 好 Plan vs 坏 Plan（供提示词/复盘） |

**运行**（testnet 示例）：

```powershell
Copy-Item examples\signals\01-open-long-tpsl.json inbox\llm-test\
.venv\Scripts\python.exe -m gate_bot once --bot llm-test
```

**必读**：`templates/README.md`（字段全集）；突破用 `stop_entry_*`，止损用 `sl`。
