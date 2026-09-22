---
feature: gate-signal-bot
status: delivered
updated: 2026-03-02
branch: main
commits: 6e3a77e..HEAD
---

# Gate Signal Bot — 策略 JSON 信号下单机器人

## Report

**What was built** — 独立项目 `gate-signal-bot`：单进程多机器人守护，扫描 `inbox/<bot_id>/*.json` 策略意图（open_long/short、close/close_all、cancel_all/cancel_price_all、hold、grid，以及 `orders[]` 多腿），按 `size_usd` 与合约 `quanto_multiplier` 换算张数，自动识别持仓模式（single/dual）并适配开平仓 body，开仓后自动挂限价止盈/止损触发单（nested `{initial,trigger}`）。支持 market/limit/post_only/ioc/fok，live/testnet 双环境独立密钥，无 dry-run。成功归档 `archive/done/`（+result.json），失败归档 `archive/failed/`（+error.json 含 partials）。CLI：`run|once|process|status`。

**Verification** — `python -m unittest discover -s tests`：**23 tests PASS**（schema/empty-action/empty-orders/grid、sizing、executor TP/SL + dual close + cancel_all、GateClient 签名头/持仓模式/合约元数据/凭据、watcher 归档与 staged 文件名）。`python -m gate_bot status`：PASS。独立复审确认 5 项 critical 全部修复、无新增 critical。

**Journey log** —
1. 触发单 body 先写成 flat 字段，对齐 `quick_order.cmd_trigger_order` 后改为 nested `{initial,trigger}`，并补 `is_stop_order`。
2. 复审指出空 `action`/空 `orders[]` 必须是合法 hold→done；`cancel_all` 无 symbol 不能只扫持仓。
3. 测试要覆盖「实现存在」之外的验收点（签名头、dual close size 符号），否则 spec 的 acceptance 无法关闭。
4. 环境无系统 Python，用 `uv venv` + `.venv/Scripts/python` 跑 unittest。
5. 并行工具调用易触发 flood 取消，修复应小步单改。

## [S1] Problem

AI/策略按计划输出交易意图 JSON，需要有机器人**自动识别并直接在 Gate.io 下单**。现有 `pa-data-source-v2.11/quick_order.py` 是人工 CLI，不支持：

1. 文件夹投递 JSON、无人值守执行；
2. 多机器人并行（不同密钥 / 不同品种 / 不同风控）；
3. 策略意图层（`size_usd` + `tp/sl`）→ 自动换算张数并挂止盈止损；
4. 网格 / 多腿一文件批量执行。

约束（用户已定）：

- 输入 = **指定文件夹**，AI **定时写入 JSON**；
- 多机器人 = **子目录隔离**（`inbox/<bot_id>/*.json`）；
- 代码落在**独立项目** `gate-signal-bot`（不耦合数据源技能）；
- **不做 dry-run**；必须支持 **实盘 + 模拟盘双模式**；
- 持仓模式（single/dual/dual_long_short）由脚本**根据密钥自动识别**；
- 覆盖各种订单类型（market/limit/post_only/ioc/fok）与计划委托/止盈止损；
- 信号层支持 **orders[] 多腿** 与 **grid 简写**。

## [S2] Design

### 架构

```text
AI 定时写 JSON ──► inbox/<bot_id>/*.json
                      │
                      ▼
              gate_bot.watcher（单进程守护，多 bot 扫描）
                      │  解析/校验/展开 grid
                      ▼
              gate_bot.executor（换算张数、适配持仓模式、调 Gate REST）
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   POST orders   POST price_orders  DELETE orders/price_orders
   （开/平）      （自动 TP/SL）      （撤单）
                      │
          成功 → archive/done/<bot_id>/
          失败 → archive/failed/<bot_id>/ + reason.json
```

- **单进程多机器人**：`config/bots/<bot_id>.yaml` 注册机器人；守护循环扫描各自 `inbox/<bot_id>/`。
- **密钥隔离**：每 bot 独立 `env`（`live`|`testnet`）与 API Key；测试网密钥变量 `GATE_TESTNET_API_KEY/SECRET`，实盘 `GATE_API_KEY/SECRET`。启动/每单打印环境横幅，防误下单。密钥优先环境变量，yaml 可引用变量名而非明文。
- **持仓模式自动识别**：每笔单前调 `GET /api/v4/futures/usdt/account` 取 `position_mode`（短 TTL 缓存 30s），映射：
  - `single`：开反向 = 自动平仓反向；`close` 自动识别方向；
  - `dual` / `dual_long_short` / `dual_plus`：开平需明确 long/short；平仓 TP/SL 的 `side` 与持仓相反。
- **合约元数据**：`GET /api/v4/futures/usdt/contracts` 拉取 `quanto_multiplier` / `order_size_round` / `order_price_round` / `leverage_max`，用于 `size_usd → 张数` 换算与精度取整。缓存 1h。

### 信号 JSON 契约（策略意图层）

文件名任意（建议 `YYYYMMDD-HHMMSS-<uuid>.json`），UTF-8。合法形态二选一：

**A. 单意图（顶层 `action`）**

```json
{
  "action": "open_long",
  "symbol": "BTC_USDT",
  "size_usd": 100,
  "type": "market",
  "price": null,
  "leverage": 5,
  "tp": 75000,
  "sl": 72000,
  "tp_type": "limit",
  "sl_type": "limit",
  "trigger_price_type": "mark",
  "label": "drive",
  "meta": {"strategy": "deepseek-v1", "confidence": 85}
}
```

**B. 多意图（顶层 `orders` 数组）** — 网格 / 多腿 / 多品种

```json
{
  "orders": [
    {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 50, "type": "limit", "price": 70000},
    {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 50, "type": "limit", "price": 69500},
    {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 50, "type": "limit", "price": 69000, "tp": 72000, "sl": 68000}
  ]
}
```

**C. 网格简写（顶层 `action: "grid"`）** — 展开为 B

```json
{
  "action": "grid",
  "symbol": "BTC_USDT",
  "side": "long",
  "levels": [
    {"price": 70000, "size_usd": 50},
    {"price": 69500, "size_usd": 50},
    {"price": 69000, "size_usd": 50}
  ],
  "type": "limit",
  "tp": 72000,
  "sl": 68000
}
```

#### action 集合

| action | 语义 | 必填 | 说明 |
|--------|------|------|------|
| `open_long` / `open_short` | 开多/开空 | symbol, size_usd 或 size | `size` 为直接张数（可选，优先于 size_usd） |
| `close` | 平仓 | symbol | single 自动识别方向；dual 需 `side: long\|short`；可选 `size` 张数，缺省全平 |
| `close_all` | 市价全平 | symbol 可选 | 不带 symbol 则全品种 |
| `cancel_all` | 撤销普通挂单 | symbol 可选 | |
| `cancel_price_all` | 撤销计划委托 | symbol 可选 | |
| `hold` / 空 action | 无操作 | — | 直接归档 done |
| `grid` | 网格多档限价开仓 | symbol, side, levels[] | 展开为多条 open_* |

#### 意图字段

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `symbol` | string | — | `BTC_USDT` 或 `BTC`（经 SYMBOL_MAP 归一） |
| `size_usd` | number | — | 名义 USDT；`size` 存在时忽略 |
| `size` | int | — | 直接张数（高级） |
| `type` | string | `market` | `market\|limit\|post_only\|ioc\|fok` → TIF `ioc/gtc/poc/ioc/fok` |
| `price` | number | null | type≠market 时必填 |
| `leverage` | int | 不改 | 若给出则先 `POST /leverage` |
| `tp` | number | null | 止盈触发价；开仓成功后自动挂 close-trigger |
| `sl` | number | null | 止损触发价 |
| `tp_type` / `sl_type` | string | `limit` | 触发后 market/limit；**真实平仓触发禁止 market**（与 quick_order 一致，强制 limit） |
| `tp_limit_price` / `sl_limit_price` | number | tp/sl ± 滑点偏移 | 触发后限价；缺省按方向偏移 0.1% |
| `trigger_price_type` | string | `latest` | `latest\|mark\|index` |
| `trigger_rule_tp` | int | 自动 | 多头止盈=1、止损=2；空头对调 |
| `trigger_expiration` | int | null | 过期秒数；**testnet 自动忽略** |
| `margin_mode` | string | — | `cross\|isolated` |
| `label` | string | `signal` | 写入 `text: t-<label>`，便于对账 |
| `orders` | array | — | 多意图；与顶层 action 互斥 |
| `levels` | array | — | grid 档位 |
| `meta` | object | — | 透传审计，不参与下单 |

#### 张数换算

```text
contracts = floor( size_usd / (entry_price * quanto_multiplier) )
contracts = max(1, round_to(contracts, order_size_round))
```

- `entry_price`：limit/post_only/ioc/fok 用 `price`；market 用最新价（`GET /futures/usdt/tickers` 最近价）。
- 换算结果写入执行日志（size_usd、price、multiplier、contracts）。
- `size_usd < 一档面值` → 失败归档，不强行下 0 张。

#### 订单类型映射（与 quick_order 一致）

| type | body |
|------|------|
| market | `price=0`, `tif=ioc` |
| limit | `price=P`, `tif=gtc` |
| post_only | `price=P`, `tif=poc` |
| ioc | `price=P`, `tif=ioc` |
| fok | `price=P`, `tif=fok` |

开仓 size 符号：long>0 / short<0。自动 TP/SL：

```text
POST /api/v4/futures/usdt/price_orders
{
  contract, size: <相反方向张数或 0=全平>,
  price: <tp_limit_price>,          # limit；market 时 price=0, initial.tif=ioc
  trigger_price: <tp>,
  rule: 1|2,                        # 按方向自动
  price_type: 0|1|2,                # latest/mark/index
  strategy_type: ...,
  is_stop_order: true|false
}
```

`--close-trigger` 语义 = 平仓触发单（禁止 trigger market）。

#### 多 bot 配置 `config/bots/<bot_id>.yaml`

```yaml
enabled: true
env: live          # live | testnet
api_key_env: GATE_API_KEY
api_secret_env: GATE_API_SECRET
symbols:           # 白名单，空=不限
  - BTC_USDT
  - ETH_USDT
max_notional_usd: 500      # 单笔名义上限，超出拒单
max_orders_per_file: 20
max_files_per_run: 50
poll_interval_sec: 2
```

### 文件生命周期

1. 扫描 `inbox/<bot_id>/*.json`（跳过 `.*` 与非 `.json`）；
2. 读入 → 校验 → 执行；**同文件内顺序执行**，文件间按 mtime 升序；
3. 全部成功 → `archive/done/<bot_id>/<原名>` + 旁路 `*.result.json`（每腿 order_id/status）；
4. 任一步失败 → `archive/failed/<bot_id>/<原名>` + `<原名>.error.json`（code/message/step）；已成功的腿不回滚，error 中列出 partials；
5. 非法 JSON / schema 失败 → 直接 failed，不下单；
6. 原子性：先 `os.replace` 到处理中临时名再执行，避免半读。

### 错误行为

| 情况 | 行为 |
|------|------|
| 缺密钥 / env 不匹配 | 启动即拒；单文件 failed，横幅标明 live/testnet |
| 品种不在白名单 | 拒单 failed |
| size_usd 超 max_notional_usd | 拒单 failed |
| 合约不存在 / 面值换算为 0 | failed |
| Gate API 4xx/5xx | failed，保留 `label` 原文便于对账 |
| 网络超时 | failed + error 注明 timeout（**不自动重试**，防重复下单）；AI 下轮可重发 |
| hold / 空 orders | done（审计可见） |

### CLI

```text
python -m gate_bot run              # 守护：扫描全部 enabled bots
python -m gate_bot run --bot alpha  # 仅一个 bot
python -m gate_bot once --bot alpha # 跑一轮后退出（便于计划任务）
python -m gate_bot process <file> --bot alpha  # 执行单文件
python -m gate_bot status           # bot 配置/inbox 余量/最近执行摘要
```

### 复用来源（从 pa-data-source-v2.11 抽取并库化）

- `rest_signed_request` / `gate_sign`（quick_order.py:202–261）
- `resolve_symbol` / `SYMBOL_MAP` / `ORDER_TIF` / `apply_order_type` 语义
- `api_get_position_mode` 与 dual 判定（quick_order.py:339–344, 166–167）
- 开/平/触发/撤 REST 路径与 body 形状（quick_order cmd_open / cmd_close / cmd_price_trigger / cancel_*）
- 环境选择 live/testnet 的 REST 基址与密钥变量约定

### 测试边界

- schema 解析：合法 A/B/C、缺字段、互斥字段、非法 type；
- grid 展开为 orders；
- size_usd 换算（mock contracts meta + ticker）；
- 触发规则自动推导（多/空 × tp/sl）；
- 文件归档 done/failed 路径；
- 持仓模式适配 body（single vs dual 的 close side）；
- **不**对真实 Gate 签名做网络集成测试（手工用 testnet 验证）。

## [S3] Out of Scope

- 不做行情采集 / K 线库（pa-data-source 职责）；
- 不做 AI 策略与信号生成（只消费 JSON）；
- 不做 HTTP/Webhook 接入（仅文件夹）；
- 不做 dry-run 模式；
- 不做订单改单（amend）、追踪止损 trail、BBO；
- 不做自动重试/补单；
- 不做 GUI；
- 不跨 bot 共享密钥或仓位。

## Tasks

- [x] T1: 核心库 gate_bot/gate_client.py — 签名 REST、环境选择、合约元数据、持仓模式识别 — acceptance: 单元测试可 mock 调用 get position_mode/contracts，签名请求构造正确 (covers: S2)
- [x] T2: 信号解析 gate_bot/schema.py — 单意图/多意图/grid 展开、字段校验、触发规则推导 — acceptance: 合法 A/B/C 解析通过；非法 action/互斥字段抛 SchemaError (covers: S2)
- [x] T3: 张数换算 gate_bot/sizing.py — size_usd→contracts，精度/最小 1 张/不足面值失败 — acceptance: 给定 mock multiplier 与 price 得到期望张数；过小失败 (covers: S2)
- [x] T4: 执行器 gate_bot/executor.py — 开/平/撤 + 自动 TP/SL 价格触发单 + 持仓模式适配 — acceptance: mock API 下 open_long+tp+sl 产生 1×orders + 2×price_orders，dual close 带正确 side (covers: S2)
- [x] T5: 多 bot 配置与文件守护 gate_bot/config.py + gate_bot/watcher.py — 扫描 inbox、归档 done/failed、result/error 旁路 — acceptance: 合法文件归档 done 并写 result.json；失败归档 failed 并写 error.json (covers: S2)
- [x] T6: CLI 入口 gate_bot/__main__.py — run/once/process/status — acceptance: `python -m gate_bot status` 列出 bots 与 inbox 积压 (covers: S2)
- [x] T7: 配置样例与 README — config/bots/_example.yaml、目录约定、JSON 样例 — acceptance: 新用户按 README 能写入 inbox 并跑 once (covers: S2)
- [x] T8: 测试套件 tests/ — schema/sizing/executor/watcher 关键路径 — acceptance: `python -m unittest` 全绿 (covers: S2)
