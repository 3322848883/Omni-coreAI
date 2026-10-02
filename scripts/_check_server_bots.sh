#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
echo "=== 服务器在跑的 bot ==="
ps -eo args | grep '[g]ate_bot' | grep -E 'plan-loop|paper-run|run --bot' | while read -r line; do
  bot=$(echo "$line" | sed -n 's/.*--bot \([^ ]*\).*/\1/p')
  mode=$(echo "$line" | grep -oE 'plan-loop|paper-run|run --bot' | head -1)
  echo "  $bot / $mode"
done | sort | uniq

echo "=== enabled=true 的 bot ==="
cd /opt/omnialpha
grep -l "enabled: true" config/bots/*.yaml 2>/dev/null | while read -r f; do
  n=$(basename "$f" .yaml)
  case "$n" in _*) continue;; esac
  echo "  $n"
done

echo "=== CHECK_DONE ==="
