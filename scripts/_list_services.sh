#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "===================================================="
echo "  服务器在跑的项目/服务  $(date '+%Y-%m-%d %H:%M')"
echo "===================================================="
free -m | head -2
echo ""

echo "=== 1) systemd 服务（按内存排序 TOP 25）==="
ps -eo pid,rss,args --sort=-rss | grep -v grep | head -26 | awk 'NR==1{next} {printf "%7.0f MB  pid=%-8s %s\n", $2/1024, $1, substr($0, index($0,$3), 70)}'

echo ""
echo "=== 2) Docker 容器 ==="
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" 2>/dev/null | head -20

echo ""
echo "=== 3) 按可执行文件归类（内存合计）==="
ps -eo comm,rss --no-headers | awk '{
  gsub(/[0-9]/,"",$1); name=$1;
  sum[name]+=$2; n[name]++
} END {
  for (k in sum) printf "%7.0f MB  x%-4d %s\n", sum[k]/1024, n[k], k
}' | sort -rn | head -20

echo ""
echo "=== 4) /opt 下的项目目录 ==="
du -sh /opt/* 2>/dev/null | sort -rh | head -15

echo ""
echo "=== 5) 监听端口 ==="
ss -tlnp 2>/dev/null | grep LISTEN | awk '{print $4, $6}' | sed 's/users:((//' | sed 's/))//' | head -20

echo "=== LIST_DONE ==="
