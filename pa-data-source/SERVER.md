# pa-data-source 服务器部署指南（Linux）

> pa-data-source 支持 Windows 与 Linux 服务器长期运行。本文件是 Linux 服务器的完整部署说明。

## 1. 支持环境

| 项 | 要求 |
|----|------|
| 系统 | Ubuntu 20.04+/Debian 11+/CentOS 7+ 等主流 Linux |
| 架构 | x86_64（amd64）或 arm64 |
| Python | 3.8+（含 `venv` 模块） |
| 网络 | 出站 443：`api.gateio.ws`（REST）、`fx-ws.gateio.ws`（WebSocket） |
| 磁盘 | 首次全量约 100MB，长期约 200-500MB（含日志轮转） |
| 内存 | 256MB 起（建议 512MB+） |

## 2. 快速部署（推荐）

把技能目录上传到服务器后，在技能根目录执行：

```bash
bash server_setup.sh
```

脚本自动完成：
1. 创建虚拟环境 `.venv` 并安装依赖（pyyaml、websocket-client）
2. 下载对应平台的 **Linux 版 gate-cli**（官方 GitHub Releases，SHA256 校验）
3. 创建 `data/`、`logs/` 目录

> 注意：Windows 版自带的 `gate-cli.exe` 不能在 Linux 运行，`server_setup.sh` 会下载 Linux 版 `gate-cli` 覆盖。纯行情采集不需要 gate-cli；备用数据源与账户/下单才需要。

## 3. 配置密钥（环境变量，不落盘）

```bash
export GATE_API_KEY="你的实盘或测试网 key"
export GATE_API_SECRET="你的 secret"
```

未配置密钥时：行情数据（公开接口）照常工作，账户推送自动关闭。
切换测试网/实盘有两种方式（二选一）：
```bash
# 方式一：环境变量（推荐，服务器免改配置）
export PA_DATA_SOURCE_ENV=testnet     # 或 live
# 方式二：编辑 watchlist.yaml 的 env: live | testnet
```
测试网说明：仅支持 BTC/ETH/SOL（XAU/XAG 自动排除）；账户/行情 REST 可用；
测试网实时 WebSocket 通道可能 502（Gate 侧），由 REST 校准兜底更新。

## 4. 启动与验证

### 前台启动（测试）
```bash
./.venv/bin/python watchdog.py
```

### 验证运行状态
```bash
curl http://127.0.0.1:18080/health
```
返回 `status: running` + 各品种/周期 K 线数量 + 最新价 + 账户状态即正常。

### 数据输出
- 行情：`data/kline.db`
- 账户：`data/account.db`
- 日志：`logs/kline_watcher.log`（按天轮转，保留 7 天）、`logs/kline_watcher_child.log`（子进程崩溃现场）

## 5. systemd 开机自启（推荐）

创建 `/etc/systemd/system/pa-data-source.service`：

```ini
[Unit]
Description=PA Data Source (Gate.io kline + account watcher)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/pa-data-source
Environment=GATE_API_KEY=你的key
Environment=GATE_API_SECRET=你的secret
ExecStart=/opt/pa-data-source/.venv/bin/python watchdog.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pa-data-source
sudo systemctl status pa-data-source
journalctl -u pa-data-source -f          # 查看日志
```

> 生产建议把密钥放 systemd `EnvironmentFile=` 或环境变量服务（如 systemd-creds / docker secret），不要写死在 unit 文件里。

### 5.1 ⚠️ 两种启动方式**只能选一种**（否则触发重启风暴）

`watchdog.py` 是「**全 4 个目标一起起**」（`TARGETS`：kline-live + kline-testnet +
kline-multi + fetch-aux），且任一子进程崩溃就整体重启。所以：

| 你想跑的 | 用哪种 | 注意 |
|---|---|---|
| **全部 4 个** | `watchdog.py`（一个 systemd 单元） | 简单，但无法只跑其中几个 |
| **只跑其中几个**（如只要 Gate、不要那 5 个非 Gate 交易所） | **按组件各建一个 systemd 单元**，各自 `Restart=always` | 此时**绝不能**再起 `watchdog.py` |

**混用会出事的机制**：`fetch_aux.py` 有 `aux-data/aux_fetch.lock`、
`kline_watcher.py` 有 `kline_watcher.lock` —— 都是**单实例锁**。两个实例抢锁时
一个起不来 → 被 watchdog 判定「崩溃」→ 整体重启 → 反复循环。
**这正是 2026-08 那次「重启风暴」的成因**（fetch_aux 退码 1 → 整体重启连锁 →
5 次/小时触顶停摆，健康端点无响应、留下孤儿锁）。

**切换顺序**（从「按组件」切到「watchdog」）：

```bash
systemctl stop omnialpha-kline-live omnialpha-kline-testnet omnialpha-aux
systemctl disable omnialpha-kline-live omnialpha-kline-testnet omnialpha-aux
# 确认锁文件已释放、无残留进程
ps aux | grep -E "kline_watcher|fetch_aux" | grep -v grep
systemctl enable --now pa-data-source        # 再起 watchdog
```

**按组件起时，建议的单元与参数**（与 `watchdog.py` 的 `TARGETS` 保持一致）：

```ini
# Gate 实盘 K 线 + 账户推送
ExecStart=/opt/omnialpha/pa-data-source/.venv/bin/python kline_watcher.py
# Gate 测试网 K 线（端口/锁/库都要错开，否则与上面抢）
ExecStart=... kline_watcher.py --env testnet --config watchlist_testnet.yaml \
          --db data/kline_testnet.db --health-port 18081 --lock kline_watcher_testnet.lock
# aux 辅助流
ExecStart=... fetch_aux.py --loop
```

**多实例的日志要分开**：`logger.py` 默认把两个 `kline_watcher` 实例都写到
`logs/kline_watcher.log`，消息会交错（实测踩过：把测试网的行当成实盘的，误判
「实盘采错品种」）。给每个实例设 `Environment=PA_KLINE_LOG=<各自的文件>` 即可分开。

## 6. 稳定性机制（服务器上同样生效）

| 机制 | 说明 |
|------|------|
| watchdog 守护 | 崩溃 5 秒自动重启，限流 5 次/小时，正常退出不重启 |
| 单实例锁 | `watchdog.lock` 锁文件，防止重复启动；进程死亡自动接管 |
| 断线重连+补全 | WebSocket 断开自动重连，REST 补全缺失 K 线 |
| CLI 备用源 | 主数据源异常连续 3 次自动切换 gate-cli 兜底 |
| 数据停滞检测 | 120 秒无更新触发告警 |
| REST 校准 | 定期对比修复 K 线漂移 |
| 日志轮转 | 按天轮转保留 7 天 |
| 数据保留 | 超期 K 线/账户历史自动清理（`watchlist.yaml` 配置） |

## 7. 与 pa-trading-system 对接

在 pa-trading-system 的 `data/config.yaml` 指向服务器的数据库：

```yaml
external_source:
  kline_db: "/opt/pa-data-source/data/kline.db"
  account_db: "/opt/pa-data-source/data/account.db"
  watchlist: "/opt/pa-data-source/watchlist.yaml"
  quick_order: "/opt/pa-data-source/quick_order.py"
```

然后运行 `python -m data.sync` 同步。

## 8. 常见问题

### gate-cli 无法运行
- 确认是 Linux 版：`./gate-cli --version`；若报 `Exec format error`，说明还是 Windows 的 .exe，重跑 `server_setup.sh`。
- 确认有执行权限：`chmod +x gate-cli`。

### 健康检查连不上
- 确认 watchdog 在跑：`ps aux | grep watchdog`。
- 确认端口没被占用：`ss -ltnp | grep 18080`。

### 账户数据为空
- 确认已设置 `GATE_API_KEY/GATE_API_SECRET` 环境变量并重启。
- 首次写入在启动后约 60 秒（账户推送周期）。

### 网络受限
- 若服务器无法直连 GitHub，可先在有网络的机器下载 `gate-cli_<ver>_linux_<arch>.tar.gz` 上传后手动解压到技能目录。

### Windows 与服务器差异
| 项 | Windows | Linux 服务器 |
|----|---------|--------------|
| 启动 | `launcher.vbs` / `python watchdog.py` | `./.venv/bin/python watchdog.py`（或 systemd） |
| gate-cli | `gate-cli.exe`（已内置） | `server_setup.sh` 自动下载 Linux 版 |
| 依赖 | 系统 Python 直接 pip install | 虚拟环境 `.venv` |

## 9. 安全提醒

- 密钥只放环境变量 / systemd EnvironmentFile，不要写入代码或提交仓库。
- 服务器仅开放必要端口（本机回环 18080 健康检查，无需对外暴露）。
- 定期轮换 API 密钥；最低权限原则（只开查询权限，除非确需下单）。
