#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/gate-signal-bot

echo "=== 1) 关掉服务器上的模拟盘（enabled: false）==="
count=0
for f in config/bots/*.yaml; do
  n=$(basename "$f" .yaml)
  case "$n" in _*) continue;; esac
  # 只保留 brooks-btc 实盘
  if [ "$n" != "brooks-btc" ]; then
    if grep -q "enabled: true" "$f"; then
      sed -i 's/^enabled: true/enabled: false/' "$f"
      echo "  OFF $n"
      count=$((count+1))
    fi
  else
    echo "  KEEP $n (实盘)"
  fi
done
echo "关闭了 $count 个 bot"

echo "=== 2) 确认只剩实盘 enabled ==="
grep -l "enabled: true" config/bots/*.yaml 2>/dev/null | while read -r f; do
  n=$(basename "$f" .yaml)
  case "$n" in _*) continue;; esac
  echo "  ON  $n"
done

echo "=== 3) 重启看门狗（只管实盘）==="
systemctl restart gate-watchdog
sleep 5
systemctl is-active gate-watchdog

echo "=== 4) 运行中的 bot ==="
ps -eo args | grep '[g]ate_bot' | grep -E 'plan-loop|paper-run|run --bot' | while read -r line; do
  bot=$(echo "$line" | sed -n 's/.*--bot \([^ ]*\).*/\1/p')
  mode=$(echo "$line" | grep -oE 'plan-loop|paper-run|run --bot' | head -1)
  echo "  $bot / $mode"
done | sort | uniq

echo "=== 5) 内存 ==="
free -m | head -2

echo "=== LIVE_ONLY_DONE ==="
