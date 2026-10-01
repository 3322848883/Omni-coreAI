#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "=== 1) Nginx 全量域名配置 ==="
nginx -T 2>/dev/null | grep -E 'server_name|proxy_pass|^\s*listen' | sed 's/^\s*//' | head -40

echo ""
echo "=== 2) /etc/hosts 自定义域名 ==="
grep -vE '^#|^$|localhost' /etc/hosts 2>/dev/null

echo ""
echo "=== 3) /opt 项目 ==="
for d in /opt/*/; do
  n=$(basename "$d")
  sz=$(du -sh "$d" 2>/dev/null | awk '{print $1}')
  desc=$(grep -m1 '"name"' "$d/package.json" 2>/dev/null | sed 's/.*: *"//; s/".*//')
  [ -z "$desc" ] && desc=$(grep -m1 'name:' "$d/docker-compose.yml" 2>/dev/null | awk '{print $2}')
  printf "  %-30s %-8s %s\n" "$n" "$sz" "$desc"
done

echo ""
echo "=== 4) 关键端口背后的实际进程 ==="
for p in 5001 3100 7863 7864 7866 8081 8085 8648 22217 7872 7873; do
  pid=$(ss -tlnp 2>/dev/null | grep ":$p " | grep -oP 'pid=\K[0-9]+' | head -1)
  if [ -n "$pid" ]; then
    args=$(ps -p $pid -o args= 2>/dev/null | cut -c1-60)
    printf "  :%-6s pid=%-8s %s\n" "$p" "$pid" "$args"
  fi
done

echo ""
echo "=== 5) 浏览器进程归属（谁启动的）==="
ps -eo pid,ppid,rss,args | grep -E '[c]hrome|[a]gent-browser|[p]laywright' | awk '{printf "  pid=%-8s ppid=%-8s %5.0fMB  %s\n", $1,$2,$3/1024, substr($0,index($0,$4),55)}' | head -12

echo "=== MAP2_DONE ==="
