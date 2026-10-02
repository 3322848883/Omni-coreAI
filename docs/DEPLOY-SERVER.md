# 服务器部署指南（Linux / systemd）

> 本文是 Windows 本机开发之外的**生产部署清单**。Windows 无窗口弹窗陷阱见 `OPERATIONS.md` §2.0b。

---

## 1. 需求

| 项 | 建议 |
|----|------|
| OS | Ubuntu 22.04+ / Debian 12 / CentOS 9 |
| Python | **3.12+**（本项目在 3.14 验证过） |
| 内存 | ≥2 GB（18 bot 时 ~1.5 GB） |
| 磁盘 | ≥20 GB（K 线库 + paper 账本会涨） |
| 网络 | 能访问 Gate API + LLM 网关 |

---

## 2. 安装

```bash
# 2.1 代码
sudo mkdir -p /opt && cd /opt
git clone https://github.com/3322848883/Omni-coreAI.git OmniAlpha
cd OmniAlpha

# 2.2 Python 虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .          # 让任意目录都能 python -m omnialpha

# 2.3 密钥（**不要**写进代码）
cat > .env <<'EOF'
OMNIALPHA_ROOT=/opt/omnialpha
GATE_API_KEY=xxx
GATE_API_SECRET=xxx
OPENAI_BASE_URL=http://69.12.85.185:7863/v1
OPENAI_API_KEY=xxx
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_USER_OPEN_ID=ou_xxx
EOF
chmod 600 .env
```

---

## 3. systemd 服务

### 3.1 看门狗（推荐，一键托管全部 enabled bot）

```ini
# /etc/systemd/system/omnialpha-watchdog.service
[Unit]
Description=OmniAlpha watchdog
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/omnialpha
EnvironmentFile=/opt/omnialpha/.env
ExecStart=/opt/omnialpha/.venv/bin/python -m omnialpha watchdog --interval 15
Restart=always
RestartSec=10
User=gate
# 无窗口（Linux 本来就没有）+ 防双开
StandardOutput=append:/opt/omnialpha/data/watchdog.out
StandardError=append:/opt/omnialpha/data/watchdog.err

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -m -d /opt/omnialpha -s /bin/bash gate
sudo chown -R gate:gate /opt/omnialpha
sudo systemctl daemon-reload
sudo systemctl enable --now omnialpha-watchdog
sudo systemctl status omnialpha-watchdog
```

### 3.2 或单独跑某个实盘 bot

```ini
# /etc/systemd/system/gate-brooks.service
[Unit]
Description=OmniAlpha brooks-btc
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/omnialpha
EnvironmentFile=/opt/omnialpha/.env
ExecStart=/opt/omnialpha/.venv/bin/python -m omnialpha supervisor --bot brooks-btc
Restart=always
RestartSec=5
User=gate

[Install]
WantedBy=multi-user.target
```

---

## 4. 与 Windows 的差异对照

| 项 | Windows | Linux |
|----|---------|-------|
| 启动 | `scripts\start_*_bg.bat` | systemd `ExecStart` |
| 无窗口 | `pythonw.exe` + DLL 三件套 | 无此问题 |
| 密钥 | `scripts/secrets.bat`（gitignored） | `.env` + `EnvironmentFile=` |
| 崩溃拉起 | 任务计划 / 看门狗 | systemd `Restart=always` |
| 单实例锁 | OS 文件锁（msvcrt） | OS 文件锁（fcntl.flock） |
| 路径 | 反斜杠，代码已兼容 | 正斜杠 |

---

## 5. 部署后自检

```bash
cd /opt/omnialpha
source .venv/bin/activate

# 5.1 状态
python -m omnialpha status

# 5.2 测试全绿
python -m unittest discover -s tests

# 5.3 实盘连通（只读）
python -m omnialpha once --bot brooks-btc

# 5.4 回测冒烟
python -m omnialpha backtest --bot brooks-btc --days 7

# 5.5 看门狗接管范围
python -c "
from pathlib import Path
from omnialpha.watchdog import Watchdog
wd = Watchdog(Path('.'))
wd.discover()
print('targets:', len(wd.targets))
"
```

**期望**：status 正常、tests 全 OK、watchdog targets = 期望 bot 组件数（如 18 bot / 36 组件）。

---

## 6. 首次上线建议

1. **先跑模拟盘 1–2 天**：`env: paper` 的 bot，观察 `paper-score`
2. **实盘小额**：`max_notional_usd ≤ 10`
3. **开飞书推送**：验证收到开/平仓卡片后再放大
4. **看门狗告警**：故意杀掉一个 worker，确认收到「↻ 自动拉起」

---

## 7. 升级 / 回滚

```bash
# 升级
cd /opt/omnialpha
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart omnialpha-watchdog

# 回滚
git log --oneline -5
git reset --hard <上一个稳定 commit>
sudo systemctl restart omnialpha-watchdog
```

**注意**：升级会触发 `uv venv` 重建的话，Linux 无 pythonw 弹窗问题；Windows 见 OPERATIONS.md §2.0b。

---

## 8. 安全清单

| 项 | ✅ 已做 | 你要做的 |
|----|--------|---------|
| 密钥不入 git | `.gitignore` + bat 脱敏 | `.env` 权限 `600` |
| 交易所 key 无提币权 | — | 在 Gate 后台限制 |
| 实盘小额 | 默认 max_notional 小 | 逐步放量 |
| 告警推送 | 飞书/Telegram 可选 | 建议开启 |
