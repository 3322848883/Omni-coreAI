#!/bin/bash
set -e
cat > /etc/systemd/system/omnialpha-kline-live.service.d/engine.conf <<'EOF'
[Service]
Environment=PA_INDICATOR_ENGINE=v2
EOF
systemctl daemon-reload
systemctl restart omnialpha-kline-live
sleep 12
echo "active: $(systemctl is-active omnialpha-kline-live)"
PID=$(systemctl show -p MainPID --value omnialpha-kline-live)
tr '\0' '\n' < "/proc/$PID/environ" | grep PA_INDICATOR_ENGINE || echo "(env 未生效)"
echo "--- 最近日志 ---"
journalctl -u omnialpha-kline-live --since '-2 min' --no-pager | tail -6
