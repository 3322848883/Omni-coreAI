#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
echo "=== 1) 内存/交换 ==="
free -m
echo ""
echo "=== 2) CPU 负载（1/5/15 分钟）==="
uptime
echo ""
echo "=== 3) 本项目内存占用 ==="
ps -eo pid,rss,args | grep '[g]ate_bot' | awk '{sum+=$2; n++} END {printf "  进程数: %d\n  总内存: %.0f MB\n", n, sum/1024}'
echo ""
echo "=== 4) 内存占用 TOP 10 ==="
ps -eo pid,rss,args --sort=-rss | head -11 | awk '{printf "  %6.0f MB  %s\n", $2/1024, substr($0, index($0,$3), 50)}'
echo ""
echo "=== 5) 最近是否 OOM / 崩溃 ==="
dmesg 2>/dev/null | grep -iE 'oom|killed process' | tail -3 || echo "  (无)"
journalctl -u gate-watchdog --since "10 min ago" 2>/dev/null | grep -iE 'restart|fail|error' | tail -5 || echo "  (服务日志无异常)"
echo ""
echo "=== 6) bot 是否都活着（对比 36 目标）==="
ps -eo args | grep '[g]ate_bot' | grep -cE 'plan-loop|paper-run|run --bot|supervisor'
echo "=== LOAD_DONE ==="
