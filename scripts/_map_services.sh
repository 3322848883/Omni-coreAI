#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "===================================================="
echo "  服务 ↔ 域名 ↔ 端口 ↔ 内存 对照表"
echo "===================================================="

echo ""
echo "=== 1) Nginx 域名配置（server_name）==="
grep -rhE 'server_name|proxy_pass|listen ' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | grep -vE '^\s*#' | sed 's/^\s*//' | sort | uniq | head -60

echo ""
echo "=== 2) Docker 容器 → 端口映射 ==="
docker ps --format "{{.Names}}|{{.Ports}}|{{.Image}}" 2>/dev/null | sort

echo ""
echo "=== 3) 监听端口 → 进程 ==="
ss -tlnp 2>/dev/null | grep LISTEN | awk '{print $4, $6}' | sed 's/users:((//g; s/))//g' | sort -t: -k2 -n

echo ""
echo "=== 4) /opt 项目一览 ==="
for d in /opt/*/; do
  n=$(basename "$d")
  sz=$(du -sh "$d" 2>/dev/null | awk '{print $1}')
  readme=$(ls "$d"README* "$d"readme* "$d"package.json "$d"docker-compose.yml 2>/dev/null | head -1)
  desc=""
  if [ -f "$d/package.json" ]; then
    desc=$(grep -m1 '"name"' "$d/package.json" 2>/dev/null | sed 's/.*: *"//; s/".*//')
  fi
  printf "  %-28s %-8s %s\n" "$n" "$sz" "$desc"
done

echo ""
echo "=== 5) systemd 自定义服务 ==="
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | awk '{print $1}' | grep -vE '^(ssh|cron|dbus|systemd|network|polkit|rsyslog|unattended|snap|multipath|getty|containerd|docker|nginx|postgres|redis)' | head -20

echo "=== MAP_DONE ==="
