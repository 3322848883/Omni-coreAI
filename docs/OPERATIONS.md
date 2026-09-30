# 持续运行 / 生产运维（Operations）

设计上 `plan-loop` 与 `run` 是**长驻进程**，需由操作系统或服务管理器保活。  
**AI 对话环境不能当生产宿主**（进程会被会话回收）；下面才是正式跑法。

---

## 1. 进程模型（必须成对）

```text
┌─────────────────┐      inbox/<bot_id>/      ┌─────────────────┐
│  plan-loop      │ ───────────────────────► │  run            │
│  每 15 分钟 AI  │                           │  轮询执行/下单  │
└─────────────────┘                           └─────────────────┘
        │                                            │
        └──────── logs/brooks-btc-*.log / trades ────┘
```

- **只跑 `plan-loop`**：只分析、只写 inbox，不下单。  
- **只跑 `run`**：只吃 inbox 里的信号下单。  
- **正式实盘**：两个都要，且**各只开一个实例**（防重复下单）。

### 推荐：统一 Supervisor（多 bot 一键托管）

```powershell
python -m gate_bot migrate          # 旧目录 → data/bots/<id>/*
python -m gate_bot supervisor       # 启动所有 enabled bot 的 plan-loop + run
python -m gate_bot status           # 含 heartbeats / PID
# 无控制台窗口（pythonw）
.\scripts\start_brooks_btc_bg.bat
```

- 每 bot 每组件 **OS 文件锁**（`data/bots/<id>/state/*.lock`）防双开  
- 崩溃约 5s 拉起；**≤5 次/小时** 防重启风暴  
- **子进程使用 `pythonw.exe`**，不弹黑框  

### 看门狗（watchdog）—— 兜底补拉 + 飞书通知

Supervisor 只管自己启动的那组；**看门狗**是从全局视角扫「该跑的组件」，发现缺失就补拉。

```powershell
.\scripts\start_watchdog_bg.bat          # 无窗口
python -m gate_bot watchdog --interval 15
```

| 项 | 行为 |
|----|------|
| **在管判据** | bot yaml 的 `enabled: true`（唯一开关；false 一律不看） |
| 组件映射 | plan→`plan-loop`；paper→`paper-run`；live→`run` |
| 存活判定 | 探 OS 文件锁：被持有 = 活着 |
| 重启限频 | 单组件 ≤5 次/小时，超了停手 + 推送「请人工介入」 |
| 飞书通知 | 启动接管、自动拉起、崩溃风暴 |

**新增 bot**：把 `enabled: true` 即可被看门狗接管；`enabled: false` 立即不管。

### 单实例锁（PidLock）

- 互斥来自 **OS 文件锁**（Windows `msvcrt.locking` / Unix `fcntl.flock`），进程死亡自动释放
- 锁文件里的 PID 只作诊断，**不参与判定**（防 Windows PID 复用误拒）
- **worker 自己持锁**（不是 supervisor 代持）——supervisor 死了、子进程仍在时，新实例无法双开
- 存储：`data/bots.db` 台账 + `data/bots/<id>/` 文件树  
- 详细设计：`docs/compose/spec/runtime-upgrade.md`

---

## 2. Windows：开机自启 + 崩溃拉起

### 2.0 必做：关掉 Windows Terminal 套壳

Win11 默认终端若是 Windows Terminal，启动会弹黑框。改注册表为「控制台主机」：

```powershell
$p = "HKCU:\Console\%%Startup"
New-Item $p -Force | Out-Null
Set-ItemProperty $p DelegationConsole "{00000000-0000-0000-0000-000000000000}"
Set-ItemProperty $p DelegationTerminal "{00000000-0000-0000-0000-000000000000}"
```

### 2.0b 必做：uv venv 的 pythonw 弹窗陷阱

**现象**：每个 worker 都弹一个控制台窗口；或弹 `pythonw.exe - 系统错误：找不到 python314.dll`。

**根因**：`uv venv` 生成的 `.venv\Scripts\pythonw.exe` 是个 launcher 存根，实际拉起的是 uv 的 **`python.exe`（console 子系统）**——Windows 就给它一个控制台窗。若直接换成 uv 真 `pythonw.exe`，又会因找不到 `python314.dll` 报错。

**修复（三项一起拷）**：

```powershell
$uv = "C:\Users\<user>\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-none"
Copy-Item "$uv\pythonw.exe"   .venv\Scripts\ -Force   # 真 pythonw（windows 子系统）
Copy-Item "$uv\python314.dll" .venv\Scripts\ -Force   # 依赖 DLL
Copy-Item "$uv\python3.dll"   .venv\Scripts\ -Force
```

**验证**：`Get-Process python | Where-Object { $_.CommandLine -match 'gate_bot' }` 应为 **0**；`pythonw` 数量 = worker 数。

**注意**：用 `uv venv` / `uv sync` 重建环境后**会复发**，重建完必须重做这一步。

### 2.1 推荐启动

```powershell
.\scripts\start_brooks_bg.cmd   # 带密钥、pythonw、无窗口
# 或
cscript scripts\run_brooks_bg.vbs
```

### 2.2 任务计划程序（崩溃拉起）

```powershell
$wd = "C:\Users\w6485\Desktop\测试\gate-signal-bot"
$py = "$wd\.venv\Scripts\python.exe"

# 登录时启动 + 失败重试（管理员 PowerShell）
foreach ($pair in @(
  @{Name="gate-bot-plan"; Args="-m gate_bot plan-loop --bot brooks-btc"},
  @{Name="gate-bot-run";  Args="-m gate_bot run --bot brooks-btc"}
)) {
  $act = New-ScheduledTaskAction -Execute $py -Argument $pair.Args -WorkingDirectory $wd
  $trg = New-ScheduledTaskTrigger -AtLogOn
  $set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $pair.Name -Action $act -Trigger $trg -Settings $set `
    -Description "gate-signal-bot $($pair.Name)" -Force
}
```

说明：
- `MultipleInstances IgnoreNew`：**禁止双开**（防重复分析/重复下单）  
- `RestartCount 999`：进程退出后约 1 分钟自动拉起  
- `ExecutionTimeLimit 0`：不限制运行时长  

### 2.2 或用服务封装（NSSM / WinSW）

```powershell
nssm install GateBotPlan "C:\...\gate-signal-bot\.venv\Scripts\python.exe" "-m" "gate_bot" "plan-loop" "--bot" "brooks-btc"
nssm install GateBotRun  "C:\...\gate-signal-bot\.venv\Scripts\python.exe" "-m" "gate_bot" "run" "--bot" "brooks-btc"
# AppDirectory 设为仓库根；Environment 里加 OPENAI_* / GATE_*
nssm start GateBotPlan; nssm start GateBotRun
```

### 2.3 会话级脚本（你自己开的终端，关终端会停）

```powershell
.\scripts\start_brooks_btc.bat   # 启动
.\scripts\stop_brooks_btc.bat    # 停止
```

适合试运行，**不适合无人值守过夜**。

---

## 3. Linux 服务器（systemd）

`/etc/systemd/system/gate-bot-plan.service`：

```ini
[Unit]
Description=gate-signal-bot plan-loop
After=network-online.target

[Service]
WorkingDirectory=/opt/gate-signal-bot
Environment=GATE_BOT_ROOT=/opt/gate-signal-bot
Environment=OPENAI_BASE_URL=https://api.deepseek.com/v1
Environment=OPENAI_API_KEY=...
Environment=GATE_API_KEY=...
Environment=GATE_API_SECRET=...
ExecStart=/opt/gate-signal-bot/.venv/bin/python -m gate_bot plan-loop --bot brooks-btc
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`gate-bot-run.service` 把 `ExecStart` 换成 `run --bot brooks-btc`。

```bash
systemctl daemon-reload
systemctl enable --now gate-bot-plan gate-bot-run
journalctl -u gate-bot-plan -f
```

---

## 4. 防双开 / 防重复下单

| 风险 | 做法 |
|------|------|
| 两个 `run` 同时吃 inbox | 任务计划 `IgnoreNew` / systemd 默认单实例 / 先杀旧进程再启 |
| 两个 `plan-loop` 同时写 inbox | 同上；`last_cycle.json` 有 cycle 去重兜底 |
| 手工 `once` 与 `run` 并行 | 短时调试可以；正式只留一个 `run` |

查看是否双开：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'gate_bot' } |
  Select-Object ProcessId, CommandLine
```

---

## 5. 环境变量（进程必须能看到）

| 变量 | 用途 |
|------|------|
| `GATE_API_KEY` / `GATE_API_SECRET` | 实盘下单 |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | LLM |
| `GATE_BOT_ROOT` | 仓库根（systemd/cron 用） |
| `GATE_BOT_PA_DATA` | 行情库目录（可选） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_USER_OPEN_ID` | 飞书推送（应用机器人） |
| `FEISHU_WEBHOOK` | 飞书推送（群机器人，二选一） |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram 推送（可选） |

**不要**把密钥写进 yaml。服务/任务计划的「环境」或系统用户环境变量里配置。
飞书凭据也可放 `config/alerts.yaml`（已 gitignore，不入 git）。

---

## 6. 日常检查清单

| 频率 | 动作 |
|------|------|
| 每天 | `python -m gate_bot status`；看 `logs/*plan.err` / `run.err` |
| 每天 | 看 `data/bots/*/state/alerts.json` 是否有新告警（权益/重复成交/孤儿） |
| 有单时 | `python -m gate_bot trades --bot brooks-btc --tail 20` |
| 失败 | `archive/failed/brooks-btc/*.error.json` |
| 异常空转 | 确认两进程都在、OPENAI/GATE 余额与权限 |
| 告警推送 | 飞书是否收到开平仓卡片；没收到先查 `config/alerts.yaml` |

---

## 7. 停止 / 重启

```powershell
# 任务计划
Stop-ScheduledTask -TaskName gate-bot-plan -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName gate-bot-run  -ErrorAction SilentlyContinue
# 或 scripts\stop_brooks_btc.bat

# Linux
systemctl stop gate-bot-plan gate-bot-run
```

改配置后重启进程才会生效（yaml 启动时加载）。

---

## 8. 为什么不能用 AI 聊天窗口「挂着跑」

| 环境 | 适合 |
|------|------|
| AI 会话 / 交互终端 | 调试、单次 `plan` / `once` |
| 任务计划 / systemd / NSSM | **7×24 生产** |

长驻请用 §2 / §3。
