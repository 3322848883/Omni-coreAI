#!/bin/bash
set -e
mkdir -p /etc/systemd/system/omnialpha-kline-live.service.d
cat > /etc/systemd/system/omnialpha-kline-live.service.d/engine.conf <<'EOF'
[Service]
Environment=PA_INDICATOR_ENGINE=shadow
EOF
systemctl daemon-reload
systemctl restart omnialpha-kline-live
sleep 12
echo "active: $(systemctl is-active omnialpha-kline-live)"
PID=$(systemctl show -p MainPID --value omnialpha-kline-live)
echo "MAINPID=$PID"
tr '\0' '\n' < "/proc/$PID/environ" | grep PA_INDICATOR_ENGINE || echo "(env 未生效)"
tail -5 /opt/omnialpha/pa-data-source/logs/kline-live.err 2>/dev/null || true
