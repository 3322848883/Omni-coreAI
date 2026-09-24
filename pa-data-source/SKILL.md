---
name: pa-data-source
version: v2.11
tags: [gate-io, data-source, kline, websocket, real-time, futures, account, watchdog, aux-info, monitoring, node1, testnet]
description: "Gate.io 数据源技能（节点1）：行情/账户/辅助信息采集 + 监控健康检查。实盘+测试网双实例隔离。Invoke when 拉取K线、行情、账户、辅助信息、数据状态或健康检查。Do NOT invoke for 价格行为分析、交易方案、执行下单。"
---

# pa-data-source — 数据源技能（节点1）

## 节点定位
节点1=数据保障，只做采集/存储/监控，不做分析判断。节点2（pa-analysis）出方案，节点3（pa-executor）执行。辅助信息流仅旁路展示，不回流分析/执行链。

## 职责
1. 行情：Gate 合约 K 线（WebSocket 实时 + REST 回填），含 EMA20/ATR14；**实盘+测试网双实例同时常驻**，数据完全隔离
2. 账户：余额/持仓/挂单/计划委托/成交（WebSocket + REST 双通道）；实盘/测试网独立密钥、独立库
3. 辅助信息（旁路，仅展示）：新闻事件 / 市场总览 / 宏观日历 / 市场情绪 / 交易所储备 / 合约市场结构（盘口·成交·爆仓·持仓量）/ 情报数据（热度榜·公告）/ 社区舆情 / 币种基本面·技术面 / 链上数据·事件信号

## 反触发
- 不做价格行为分析/交易方案（节点2 职责）
- 不做执行/下单决策（节点3 职责）
- 辅助信息不回流节点2/3
- 不存明文密钥

## 强制原则
1. 先查后用：先 `python status.py --json` 再动作，已启动直接用，绝不重复启动
2. 单实例锁：实盘/测试网各自独立锁文件，绝不双实例写同一库
3. 数据隔离：实盘 `kline.db/account.db` 与测试网 `kline_testnet.db/account_testnet.db` 完全独立，绝不共用；aux_cache.db 独立，不碰 kline.db/account.db
4. 零脚本：采集只做拉取+去重+落盘+清理，零判断
5. 缓存不长期保存：事件按级别清理（重大24h/市场12h/单币6h），时序表14~90天
6. EMA20/ATR14 同源维护，供节点2 直接读，不重算
7. 实盘/模拟盘配置不混用：测试网实例用 `watchlist_testnet.yaml` + `GATE_TESTNET_API_KEY`，实盘用 `watchlist.yaml` + `GATE_API_KEY`

## 架构
```
watchdog.py          统一守护 kline-live + kline-testnet + fetch_aux --loop（整体重启）
kline_watcher.py     K线采集 + 账户监控（同一份代码，--env 区分实盘/测试网）
fetch_aux.py         辅助信息采集（--loop 常驻，频率调度）
aux_monitor.py       辅助流一致性判定
status.py            状态一键查询
_ws_probe.py         WS 连通性探针（诊断工具：连接/订阅/推送计数，输出 JSON 证据；复核测试网 WS 恢复情况用）
query_kline.py       K线统一查询（--env live|testnet 选库，非监控品种自动转交易所）
quick_order.py       CLI 下单工具（--env live|testnet 独立密钥，防误下单）
health_check.py      健康端点（实盘 127.0.0.1:18080 / 测试网 127.0.0.1:18081）
launcher.pyw/.vbs    GUI 启动器
install.bat/.sh      一键安装（Windows install.bat / Linux server_setup.sh）
autostart.vbs        开机自启（Startup 快捷方式指向）
watchlist.yaml       实盘配置
watchlist_testnet.yaml  测试网配置（模拟盘）
data/                kline.db + account.db（实盘）/ kline_testnet.db + account_testnet.db（测试网）
aux-data/            辅助信息运行时数据（详见 aux-data/DESIGN.md）
```

## 标准流程：先查后用
```bash
python status.py --json
```
| overall | 退出码 | 动作 |
|---|---|---|
| healthy | 0 | 直接交付，读 data/kline.db、data/account.db |
| stopped | 2 | 启动后交付：`python watchdog.py`（后台）→ 复查 status 确认 healthy |
| degraded | 1 | 先交付再加注：数据多数可用，同时告知异常字段 |

## 品种范围
本地库只存 watchlist 品种（实盘默认 BTC/ETH/SOL/XAU/XAG USDT 合约；测试网 BTC/ETH/SOL/DOGE/XRP，XAU/XAG 测试网无合约自动跳过）。统一入口 query_kline.py：
- `query_kline.py -s BTC_USDT -i 15m -n 20` 实盘本地库（含 ema20/atr14）
- `query_kline.py -s BTC_USDT -i 15m --env testnet` 测试网本地库（data/kline_testnet.db，模拟盘）
- `query_kline.py -s DOGE_USDT -i 15m` 非监控品种自动转交易所（有提示）
- `--source local|exchange` 强制来源；`--json` JSON 输出；`--list-symbols` 品种清单
- 本地：仅 watchlist 品种、2000 根、含指标、实时、落库；交易所：任意合约、默认 50 根（上限 1000）、无指标、快照、不落库
- 临时看非监控品种用 query_kline；长期监控才加 watchlist.yaml（重启回填 30~60s）

## 配置（watchlist.yaml / watchlist_testnet.yaml）
- `market_type: futures`；`env: live|testnet`（或 `PA_DATA_SOURCE_ENV=testnet` 覆盖）
- 实盘 `symbols`：BTC/ETH/SOL/XAU/XAG USDT，周期 1m~1d；`output_dir: ./data`
- 测试网 `symbols`：BTC/ETH/SOL/DOGE/XRP USDT（XAU/XAG 测试网无合约，用 DOGE/XRP 补位），周期 1m~1d
- 密钥不存明文：实盘环境变量 `GATE_API_KEY`/`GATE_API_SECRET`，测试网 `GATE_TESTNET_API_KEY`/`GATE_TESTNET_API_SECRET`（或各自 watchlist 自填）；未配置则行情照常、账户推送关闭
- 测试网密钥已配置（用户级环境变量 `GATE_TESTNET_API_KEY`/`GATE_TESTNET_API_SECRET`，2026-08-30 从 yaml 明文迁出；kline_watcher/quick_order 均环境变量优先），`account_push: true` 已开启，模拟盘账户推送正常
- 测试网 WS 官方支持实时推送（`wss://ws-testnet.gate.com/v4/ws/futures/usdt`），但当前端点不可达（10054，2026-08-29 复测仍不通），由 REST 轮询主采集兜底（10s 间隔，`TESTNET_POLL_INTERVAL`；60s 实测过慢已缩短，30 路/周期 ≈3 req/s 远低于限频）保证数据持续新鲜
- 合约存在性校验：启动时查询当前环境合约列表，不存在的品种（如测试网 XAU/XAG）自动跳过，不阻塞其他品种

## 已实测验证（2026-08-26）
- **模拟盘订单全类型**：限价/市价/PostOnly/IOC/FOK/价格触发条件单（触发后市价·限价·标记价·止盈止损）全部通过；FOK 无法全部成交返回 400 属正常拒绝
- **测试网限制**：价格触发订单 `expiration`（过期时间）参数测试网不支持（400 AUTO_INVALID_PARAM_TRIGGER_EXPIRATION），工具已加兼容提示（testnet 忽略并提示，实盘保留）
- **稳定性**：双实例 K 线 30 路连续无缺口、健康端点并发 10×200 请求成功率 100%、watchdog 故障 5s 内整体重启、辅助数据 16 类全绿（orderbook 1s / trades 7.7s 高频新鲜）

## 已实测验证（2026-08-27，v2.9）
- **trades 复合主键迁移**：v8→v9 定向迁移 `(trade_id)` → `(trade_id, contract)`（Gate 各合约成交 ID 独立编号，跨合约同 ID 在旧单字段主键下会经 INSERT OR IGNORE 静默丢数据）；事务包裹失败回滚，实测 805,613 行 100% 保留、time 索引重建、其余 16 表不动
- **账户健康检查**：健康端点 account 区块新增 `health`（configured/enabled/stale/push_mismatch/last_success_age_s），停滞判定 = 距上次成功 > max(3×interval, 180s)；配置 account_push 但未生效（push_mismatch）或数据停滞 → status 503，堵住"账户区空但 status=running"的监控盲区（08-26 夜 20h 静默停更事故的根因补丁）
- **日志计数真实性**：trades/xposts"新增 N 条"改用 `total_changes` 差值，只计真实插入（旧口径无条件 +1 把 IGNORE 去重也计成新增，虚高）

## 已实测验证（2026-08-29，v2.10）
- **测试网密钥明文清理（密钥不落盘）**：`GATE_TESTNET_API_KEY`/`GATE_TESTNET_API_SECRET` 迁入用户级环境变量（setx），watchlist_testnet.yaml 明文密钥删除；kline_watcher.py 测试网分支补环境变量优先支持（原仅读 yaml，与本文档声明不符的缺口）；迁移后实测账户推送正常（enabled、last_success ≤60s）、技能目录全量扫描无密钥残留
- **测试网 WS 复测仍不通（Gate 服务端问题）**：`wss://ws-testnet.gate.com`（官方新地址）TCP 握手即被重置（10054）；旧地址 `fx-ws-testnet.gateio.ws` 502 已废弃、`api-testnet.gateapi.io` 404 → 三候选全挂，判定服务端故障，本地无可修复项。watcher ~70s 自动重试，端点恢复后自动切回 WS 实时推送（重连后 REST 自动补全间隙），无需人工干预
- **实盘 WS 直连探针验证**：`wss://fx-ws.gateio.ws/v4/ws/usdt` 握手成功、订阅无错、15s 内收到 kline 推送；运行实例稳定连接 40+ 分钟，1m 数据年龄 14s
- **测试网 REST 轮询提速**：`TESTNET_POLL_INTERVAL` 60s→10s（60s 实测数据过慢；30 路/周期 ≈3 req/s 远低于限频），健康端点 30 路全部新鲜，1m bar 年龄 ≤40s
- **系统环境启动（脱离 TRAE 进程树）**：`autostart.vbs` 优先用技能目录 `.venv\Scripts\pythonw.exe`（系统 Python 建 venv），关 TRAE 采集不停
- **重启风暴事故复盘**：fetch_aux 退码 1 触发整体重启连锁 + 双 watchdog 竞态 → 5 次/小时触顶停摆（健康端点无响应、孤儿锁）。处置：清残留进程树+锁文件 → .venv 单实例重启恢复。注意：进程表每组件"双进程"是 venv 启动器+真实解释器的正常形态，非重复实例

## 输出（契约）
- `data/kline.db`（实盘）/ `data/kline_testnet.db`（测试网）：K 线（t/symbol/interval/OHLC/ema20/atr14），两库完全隔离
- `data/account.db`（实盘）/ `data/account_testnet.db`（测试网）：balance/position/order/price_order（current+history；price_order_* 存计划委托/止盈止损，含触发价）；挂单由 REST+WebSocket 双通道维护
- `aux-data/`：辅助信息流（snapshot/aux_cache.db/aux_status.json），详见 aux-data/DESIGN.md

## 监控与健康
| 监控面 | 机制 |
|---|---|
| 进程守护 | watchdog 多目标（kline-live + kline-testnet + fetch_aux） |
| 进程存活 | 各锁文件 PID（实盘/测试网独立锁） |
| 数据新鲜度 | 健康端点 data_freshness（实盘 30 路 = 5 品种 × 6 周期；测试网 30 路 = 5 品种 × 6 周期，fresh/stale） |
| 账户健康 | 健康端点 account.health（v2.9）：停滞（>max(3×interval,180s)）或配置-生效不匹配 → status 503 |
| 辅助流新鲜度 | aux_status.json ts 距今 ≤60s（循环粒度 5s，允许漏几轮） |
| 缓存表一致性 | aux_monitor：接口 ok ⟺ 表有数据且偏差 ≤300s、schema 版本匹配 |
| 健康端点 | 实盘 http://127.0.0.1:18080/health，测试网 http://127.0.0.1:18081/health（内嵌 aux_info，旁路不参与核心判定） |
| 日志 | logs/kline_watcher.log + logs/kline-testnet_child.log + aux-data/logs/aux_info_feed.log（按天轮转 7 天） |

status.py 总评：fetch_aux 未运行/辅助流停滞/全接口失败/缓存表不一致 ⇒ degraded（K 线主链路健康时不误报 stopped）。

## 服务器部署（Linux）
`bash server_setup.sh` + `./.venv/bin/python watchdog.py`。详见 SERVER.md。

## 与节点2 对接
节点2 features.py 读 kline.db 同源字段（ema20/atr14），不重算。节点1 只提供数据，不参与方案。

## 门禁
- 最新 K 线距当前 ≤2 分钟（按 1m 周期判定；更长周期按各自周期阈值）
- 断线自动重连 + REST 补全；watchdog 崩溃 5s 内重启（≤5 次/小时）
- 辅助流：status.py 一致性 + test_purge.py（111 项断言）

## 安全
- 不含真实密钥；密钥只从环境变量或 watchlist 读
- quick_order.py 可下单，显式调用才执行；`--env testnet` 用独立测试网密钥（GATE_TESTNET_API_KEY），实盘/模拟盘密钥不混用，操作前打印环境横幅防误下单
- 健康端点仅绑定 127.0.0.1（实盘 18080 / 测试网 18081）

## AI/程序分工附录（动作计数：程序 26 / AI 4）
细则见 `.trae/documents/节点技能-AI程序职责动作级细分与闭环完善方案-v7.md` §1.1。
- **【程序】**：watchdog 托管 / kline_watcher 采集 / REST 补全重连 / status.py 健康判定 / aux 拉取缓存 / 锁与单实例 / 健康端点 / 日志轮转（全部机械，零判断）
- **【AI】**：watchlist 品种与周期配置选择、异常告警处置意图、数据问题升级（判断性）
- **【程序→AI】证据**：health 端点 / status 摘要 / aux_status / 日志；AI 在证据上判断数据可用性
