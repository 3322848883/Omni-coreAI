# AGENTS.md — 给人 / AI 的系统入口

读完本文件应能独立使用 **gate-signal-bot**（写配置 → 投信号 / 跑 LLM 策略 → 下单）。细节再按「文档地图」下钻。

---

## 1. 这是什么

Gate.io **永续合约**信号执行 + LLM 策略层 monorepo：

```text
pa-data-source（可选，独立进程）──写──► kline.db（只读接缝）
                                            │
人 / AI 策略 ──JSON──► inbox/<bot_id>/ ──► gate_bot
                                            │  解析 → 风控 → Gate 下单
LLM strategist（可选）──Plan JSON──┘        │
                                            ▼
                                    logs/trades/*.jsonl + archive/
```

| 目录 | 职责 |
|------|------|
| `gate_bot/` | 信号执行、LLM 策略、风控、日志 |
| `pa-data-source/` | 行情采集（可选；缺库自动 REST） |
| `contracts/` | `kline.db` schema（两组件唯一接缝） |
| `config/bots/` | 每个机器人一份 yaml |
| `inbox/<bot_id>/` | 信号入口（丢 JSON 即执行） |
| `prompts/` | 可替换的策略人格 |
| `templates/` | 信号字段模板 |
| `examples/` | 可复制的信号 / 配置案例 |
| `scripts/` | 测试与上线前矩阵 |

**核心心智模型**：合法 JSON 进 inbox → **无 dry-run，直接下单**。约束靠 yaml 风控 + 程序校验，不靠模型自觉。

---

## 2. 五分钟上手

```powershell
# 1) 环境
.venv\Scripts\python.exe -m pip install -r requirements.txt   # 一般已备好
$env:GATE_TESTNET_API_KEY = "..."
$env:GATE_TESTNET_API_SECRET = "..."

# 2) 复制机器人配置
Copy-Item config\bots\_example.yaml config\bots\mybot.yaml
#    改 bot_id / env: testnet / symbols / max_notional_usd / label_prefix

# 3) 写一个信号（或抄 examples/signals/01-open-long-tpsl.json）
#    inbox\mybot\20260101-120000-demo.json

# 4) 单次执行
.venv\Scripts\python.exe -m gate_bot once --bot mybot

# 5) 看结果
.venv\Scripts\python.exe -m gate_bot status --bot mybot
#    成功：archive/done/；失败：archive/failed/*.error.json
```

LLM 策略另需 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，然后：

```powershell
.venv\Scripts\python.exe -m gate_bot plan --bot mybot     # 单轮 Plan
.venv\Scripts\python.exe -m gate_bot plan-loop --bot mybot  # 常驻策略
.venv\Scripts\python.exe -m gate_bot run --bot mybot        # 常驻执行
```

**建议路径**：`testnet` 小信号 → `prelaunch_runner` 全矩阵 → `live` 且 `max_notional_usd ≤ 10`。

---

## 3. 关键规则（写代码 / 写信号前必读）

1. **仓位优先 `size_usd`（名义 USDT）**；`size` 是合约张数，各币 1 张名义不同，用前查快照 `contract.min_notional_usd`。
2. **突破进场用 `stop_entry_*`，止损保护用 `sl`**，禁止互换。
3. **开仓必须带 `sl`**（除非 `require_sl: false`）。
4. **密钥只进环境变量**，不写 yaml、不进 git。
5. **多 bot 隔离靠 `label_prefix`**（订单 text `t-<label>`），是命名空间不是鉴权。
6. **`type=limit` 必须给 `price`**；市价遇偏离自动回退「公允价 IOC」。
7. **trail 追踪单搁置**（需资金密码）。
8. **Plan JSON 仓位字段只有 `size_usd` / `size`**；`size_pct`/`margin_pct` 是外部信号用的。

---

## 4. 文档地图（按问题找文档）

| 你想知道 | 读这个 |
|----------|--------|
| **10 分钟图文教程（推荐先做）** | `docs/TUTORIAL-10min.md` |
| 总览 / 配置 / 运行 | **`README.md`** |
| 信号字段全集、示例 | `templates/README.md` |
| 止损 vs 突破 | `templates/STOP-ENTRY-vs-STOP-LOSS.md` |
| 怎么写策略人格 | `prompts/README.md` + `prompts/vergex_default.md` |
| 可抄信号案例 | `examples/README.md`（12 个 signals + bots） |
| LLM 策略层设计 | `docs/compose/spec/llm-strategist.md` |
| 行情 hybrid / 指标 | `docs/compose/spec/market-data-hybrid.md`、`docs/reference/indicator-support-matrix.md` |
| AI 自设触发 | `docs/compose/spec/ai-event-triggers.md` |
| TP/SL 归属与挂法 | `docs/compose/spec/tpsl-ownership-revamp.md` |
| 上线前怎么测 | `docs/compose/spec/prelaunch-test.md` + `scripts/prelaunch_runner.py` |
| kline.db 格式 | `contracts/KLINE_SCHEMA.md` |
| pa 数据管道 | `pa-data-source/README.md` |
| 金额 / 张数换算 | README「张数换算」 |

---

## 5. 常用命令

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -m gate_bot status
.venv\Scripts\python.exe -m gate_bot once --bot <id>
.venv\Scripts\python.exe -m gate_bot plan --bot <id>
.venv\Scripts\python.exe scripts\prelaunch_runner.py --phase readonly --env testnet
```

---

## 6. 验证习惯

- 改代码：先跑 `unittest`（约 148 项）。
- 改下单 / 风控：跑 `scripts/prelaunch_runner.py --phase orders --env testnet`。
- 改提示词：`plan --bot` 看 reasoning 是否符合人格。
- 上线：`prelaunch --phase live` 小额闭环后才加大额度。

---

## 7. 边界与风险

- 无 dry-run：合法信号会真实下单。
- LLM 输出只是 Plan，**最终约束在 yaml 风控 + 执行器**。
- 不要把不可信 JSON 直接丢进生产 `inbox/`。
- 实盘先小额；密钥不要给提币权限。
