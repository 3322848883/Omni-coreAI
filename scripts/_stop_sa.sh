#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "=== 关闭前 ==="
free -m | head -2
echo "sa-src 容器:"
docker ps --filter "name=sa-src" --format "  {{.Names}}\t{{.Status}}"

echo ""
echo "=== 1) 停止全部 sa-src 容器 ==="
for c in $(docker ps --filter "name=sa-src" --format '{{.Names}}'); do
  echo -n "  停止 $c ... "
  docker stop "$c" >/dev/null 2>&1 && echo "OK" || echo "失败"
done

echo ""
echo "=== 2) 禁用自启（重启机器不再起来）==="
for c in $(docker ps -a --filter "name=sa-src" --format '{{.Names}}'); do
  docker update --restart=no "$c" >/dev/null 2>&1 && echo "  $c restart=no"
done

echo ""
echo "=== 3) 状态确认（数据保留）==="
docker ps -a --filter "name=sa-src" --format "  {{.Names}}\t{{.Status}}\t{{.Image}}"

echo ""
echo "=== 关闭后内存 ==="
free -m | head -2

echo ""
echo "=== 我们的 bot ==="
ps -eo args | grep '[g]ate_bot' | grep -cE 'plan-loop|paper-run|run --bot|supervisor'

echo "=== SA_STOP_DONE ==="
