#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "=== 1) 当前状态 ==="
docker inspect --format '{{.Name}} status={{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}} started={{.State.StartedAt}}' cli-proxy-api 2>&1

echo ""
echo "=== 2) 谁可能拉起它（依赖方）==="
echo "--- 其他容器的 depends_on / links ---"
for f in /opt/*/docker-compose*.yml /opt/*/*.yaml; do
  if [ -f "$f" ]; then
    hit=$(grep -l 'cli-proxy' "$f" 2>/dev/null)
    [ -n "$hit" ] && echo "  引用: $f" && grep -n 'cli-proxy' "$f" | head -5
  fi
done

echo "--- systemd 服务里有没有 cli-proxy ---"
systemctl list-units --all --no-pager 2>/dev/null | grep -i 'cli\|proxy' | head -5

echo "--- crontab ---"
crontab -l 2>/dev/null | grep -i 'cli\|proxy\|docker' | head -5

echo ""
echo "=== 3) 容器内是否嵌套 docker（拉起子容器）==="
docker inspect --format '{{.Name}} privileged={{.HostConfig.Privileged}} pid={{.HostConfig.PidMode}}' cli-proxy-api sub2api workbuddy2api 2>/dev/null

echo ""
echo "=== 4) 事件日志（谁启动了它）==="
docker events --since "1h" --until "now" --filter "container=cli-proxy-api" 2>/dev/null | head -10 || echo "  (无事件或已过期)"

echo "=== PROBE_DONE ==="
