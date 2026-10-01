#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "=== 1) 停止 ==="
docker stop cli-proxy-api >/dev/null 2>&1 && echo "  已停止"

echo "=== 2) 强制重启策略 = no ==="
docker update --restart=no cli-proxy-api

echo "=== 3) 验证策略已生效 ==="
docker inspect --format '  策略={{.HostConfig.RestartPolicy.Name}} 状态={{.State.Status}}' cli-proxy-api

echo "=== 4) 双保险：把容器改名，防止任何脚本按原名拉起 ==="
docker rename cli-proxy-api cli-proxy-api-DISABLED 2>&1 && echo "  已改名 cli-proxy-api-DISABLED"

echo "=== 5) 再次确认 ==="
docker ps -a --filter "name=cli-proxy" --format "  {{.Names}}\t{{.Status}}"
docker inspect --format '  策略={{.HostConfig.RestartPolicy.Name}} 状态={{.State.Status}}' cli-proxy-api-DISABLED

echo "=== 6) 端口 8085 是否释放 ==="
ss -tlnp 2>/dev/null | grep ':8085' || echo "  8085 已释放"

echo "=== 7) 内存 ==="
free -m | head -2

echo "=== 8) 我们的 bot ==="
ps -eo args | grep '[g]ate_bot' | grep -cE 'plan-loop|paper-run|run --bot|supervisor'

echo "=== CLI_DISABLED_DONE ==="
