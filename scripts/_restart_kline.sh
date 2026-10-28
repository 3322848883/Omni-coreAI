#!/bin/bash
set -e
systemctl restart omnialpha-kline-live
sleep 25
echo "active: $(systemctl is-active omnialpha-kline-live)"
echo "started: $(systemctl show -p ActiveEnterTimestamp --value omnialpha-kline-live)"
journalctl -u omnialpha-kline-live --since '-1 min' --no-pager | grep -i "primed" | head -4 || echo "(无 primed 日志，检查日志级别)"
