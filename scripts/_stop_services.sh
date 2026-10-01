#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "=== 关闭前内存 ==="
free -m | head -2

echo ""
echo "=== 停止容器 ==="
for c in cli-proxy-api mail-hub-mail-hub-1; do
  if docker ps --format '{{.Names}}' | grep -qx "$c"; then
    echo "  停止 $c ..."
    docker stop "$c" >/dev/null 2>&1 && echo "    OK 已停止" || echo "    失败"
  else
    echo "  $c 未在运行，跳过"
  fi
done

echo ""
echo "=== 确认状态 ==="
docker ps -a --format "{{.Names}}\t{{.Status}}" | grep -E 'cli-proxy|mail-hub' || echo "  (无)"

echo ""
echo "=== 关闭后内存 ==="
free -m | head -2

echo ""
echo "=== 我们的 bot 还活着吗 ==="
ps -eo args | grep '[g]ate_bot' | grep -cE 'plan-loop|paper-run|run --bot|supervisor'

echo "=== STOP_DONE ==="
