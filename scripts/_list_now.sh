#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "===================================================="
echo "  当前仍在运行的项目  $(date '+%H:%M')"
echo "===================================================="
free -m | head -2
echo ""

echo "=== 1) Docker 容器（运行中）==="
docker ps --format "  {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null

echo ""
echo "=== 2) 非系统进程（按内存排序 TOP 15）==="
ps -eo rss,args --sort=-rss --no-headers | grep -vE 'grep|\[' | awk '{
  cmd=substr($0,index($0,$2));
  if (cmd !~ /systemd|kworker|ksoftirq|migration|rcu_|watchdog|cpuhp|kblockd|ext4|jbd2|scsi_|md|raid|crypto|kswapd|khugepaged|kcompact|kintegrity|kdevtmpfs|kauditd|khungtask|oom_reaper|writeback|kthreadd|ksoftirqd|migration|cpuhp|watchdog|rcu_|kworker/) {
    printf "%6.0f MB  %s\n", $1/1024, substr(cmd,1,65)
  }
}' | head -15

echo ""
echo "=== 3) 按项目归类（进程数 + 内存）==="
ps -eo rss,args --no-headers | awk '{
  cmd=substr($0,index($0,$2));
  proj="其他";
  if (cmd ~ /gate-signal-bot|gate_bot/) proj="gate-signal-bot(我们的)";
  else if (cmd ~ /hermes-studio|agent-browser|ms-playwright/) proj="hermes-studio+浏览器";
  else if (cmd ~ /chrome|chromium/) proj="hermes-studio+浏览器";
  else if (cmd ~ /hermes-agent/) proj="hermes-agent";
  else if (cmd ~ /celery|uvicorn|api\.main/) proj="API服务(celery/uvicorn)";
  else if (cmd ~ /dockerd|containerd|docker-proxy/) proj="Docker引擎";
  else if (cmd ~ /postgres/) proj="PostgreSQL";
  else if (cmd ~ /nginx/) proj="Nginx";
  else if (cmd ~ /ds2api/) proj="ds2api";
  else if (cmd ~ /sub2api|subapi/) proj="sub2api";
  else if (cmd ~ /traework|twapi/) proj="traework2api";
  else if (cmd ~ /workbuddy/) proj="workbuddy2api";
  else if (cmd ~ /new-api/) proj="new-api";
  else if (cmd ~ /mail-hub/) proj="mail-hub";
  else if (cmd ~ /cli-proxy|CLIProxy/) proj="cli-proxy-api";
  else if (cmd ~ /bridge\.py|claude-bridge|codex-bridge/) proj="claude/codex-bridge";
  else if (cmd ~ /socat/) proj="socat代理";
  else if (cmd ~ /sshd/) proj="SSH";
  sum[proj]+=$1; n[proj]++
} END {
  for (k in sum) printf "%6.0f MB  x%-4d %s\n", sum[k]/1024, n[k], k
}' | sort -rn

echo ""
echo "=== 4) /opt 目录（有代码但可能没跑）==="
for d in /opt/*/; do
  n=$(basename "$d")
  sz=$(du -sh "$d" 2>/dev/null | awk '{print $1}')
  printf "  %-32s %s\n" "$n" "$sz"
done

echo "=== LIST_DONE ==="
