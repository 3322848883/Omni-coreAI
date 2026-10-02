#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/omnialpha

echo "=== 顶层 enabled 开关（bot 级）==="
for f in config/bots/*.yaml; do
  n=$(basename "$f" .yaml)
  case "$n" in _*) continue;; esac
  val=$(grep -m1 '^enabled:' "$f" | awk '{print $2}')
  echo "  $n = $val"
done

echo "=== 重启看门狗 ==="
systemctl restart omnialpha-watchdog
sleep 6

echo "=== 实际在跑的 bot ==="
ps -eo args | grep '[g]ate_bot' | grep -E 'plan-loop|paper-run|run --bot' | while read -r line; do
  bot=$(echo "$line" | sed -n 's/.*--bot \([^ ]*\).*/\1/p')
  mode=$(echo "$line" | grep -oE 'plan-loop|paper-run|run --bot' | head -1)
  echo "  $bot / $mode"
done | sort | uniq

echo "=== 内存 ==="
free -m | head -2

echo "=== DONE ==="
