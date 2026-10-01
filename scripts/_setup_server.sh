#!/bin/bash
# 配置 .env + systemd 服务
set -e
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/gate-signal-bot

echo "=== 1) .env ==="
cat > .env <<'EOF'
GATE_BOT_ROOT=/opt/gate-signal-bot
# LLM 网关（本机）
OPENAI_BASE_URL=http://127.0.0.1:7863/v1
OPENAI_API_KEY=sk-wb-Sm2NXyLm2rylSEQ7I_8HzNu_4pxmNfpoVtjcfyc9chM
# Gate 交易所（实盘需要 —— 请替换为你的 key）
GATE_API_KEY=
GATE_API_SECRET=
# 飞书告警（可选）
# FEISHU_APP_ID=
# FEISHU_APP_SECRET=
# FEISHU_USER_OPEN_ID=
EOF
chmod 600 .env
echo "ENV_OK"

echo "=== 2) systemd unit ==="
cat > /etc/systemd/system/gate-watchdog.service <<'EOF'
[Unit]
Description=gate-signal-bot watchdog (auto-restart + notify)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/gate-signal-bot
EnvironmentFile=/opt/gate-signal-bot/.env
ExecStart=/opt/gate-signal-bot/.venv/bin/python -m gate_bot watchdog --interval 15
Restart=always
RestartSec=10
User=root
StandardOutput=append:/opt/gate-signal-bot/data/watchdog.out
StandardError=append:/opt/gate-signal-bot/data/watchdog.err

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "SYSTEMD_OK"

echo "=== 3) 内存检查 ==="
free -m | head -2

echo "=== 4) 启用 bot 清单（enabled=true）==="
grep -l "enabled: true" config/bots/*.yaml | xargs -n1 basename | sed 's/.yaml//'

echo "=== SETUP_DONE ==="
