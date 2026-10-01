#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "=== 1) 找到旧版本进程 ==="
ps -eo pid,args | grep '[g]ate_bot' | grep -E '/root/ai trader|/root/trader bot|aitrader|llm-test' | head -10

echo "=== 2) 停止旧版本 ==="
for pid in $(ps -eo pid,args | grep '[g]ate_bot' | grep -E '/root/ai trader|/root/trader bot' | awk '{print $1}'); do
  echo "  kill pid=$pid"
  kill -9 "$pid" 2>/dev/null && echo "    OK" || echo "    失败"
done

echo "=== 3) 禁用自启（systemd/cron）==="
for svc in $(systemctl list-units --type=service --all --no-pager --no-legend 2>/dev/null | awk '{print $1}' | grep -iE 'aitrader|trader|llm|gate'); do
  echo "  发现服务: $svc"
  systemctl stop "$svc" 2>/dev/null && echo "    已停止"
  systemctl disable "$svc" 2>/dev/null && echo "    已禁用自启"
done
# cron 里的
crontab -l 2>/dev/null | grep -iE 'ai trader|trader bot|aitrader|llm-test|gate_bot' && echo "  (cron 里有引用，需人工确认)" || echo "  cron 无引用"

echo "=== 4) 确认残留 ==="
ps -eo args | grep '[g]ate_bot' | grep -E '/root/ai trader|/root/trader bot|aitrader|llm-test' | head -5 || echo "  (已清空)"

echo "=== 5) 我们的实盘还在吗 ==="
ps -eo args | grep '[g]ate_bot' | grep '/opt/gate-signal-bot' | grep -E 'plan-loop|run --bot'

echo "=== CLEAN_DONE ==="
