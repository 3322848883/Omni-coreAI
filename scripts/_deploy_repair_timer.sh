#!/bin/bash
# 部署 NULL 自愈 timer（每 15 分钟）
set -e
cat > /etc/systemd/system/pa-repair-nulls.service <<'EOF'
[Unit]
Description=PA data-source: repair NULL indicator columns (self-healing)

[Service]
Type=oneshot
WorkingDirectory=/opt/omnialpha/pa-data-source
ExecStart=/usr/bin/python3 /opt/omnialpha/pa-data-source/repair_nulls.py --quiet
EOF
cat > /etc/systemd/system/pa-repair-nulls.timer <<'EOF'
[Unit]
Description=PA data-source: run NULL repair every 15min

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now pa-repair-nulls.timer
systemctl start pa-repair-nulls.service || true
echo "timer: $(systemctl is-active pa-repair-nulls.timer)"
echo "last run:"
journalctl -u pa-repair-nulls.service --no-pager | tail -3
