#!/bin/bash
cd /opt/omnialpha || exit 1
echo "=== 运行中的 omnialpha 服务 ==="
systemctl list-units 'omnialpha-*' --state=running --no-legend | sed 's/^ *//' | cut -d' ' -f1

echo
echo "=== 各 bot 日志里的下单痕迹（统计） ==="
for b in $(ls data/bots); do
  log="data/bots/$b/logs/plan.err"
  [ -f "$log" ] || continue
  n=$(grep -c "place_order\|place_price_order\|order" "$log" 2>/dev/null)
  echo "$b: $n"
done

echo
echo "=== 01:20-01:45 UTC 之间 ETH/BTC 相关日志 ==="
grep -h "2026-10-29 01:[2-4]" data/bots/*/logs/plan.err 2>/dev/null \
  | grep -iE "BTC|80800|open_long|place" | tail -12
