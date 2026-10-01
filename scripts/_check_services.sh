#!/bin/bash
for s in gate-signal-bot.service gate-signal-bot-ws.service gate-signal-bot-ws-plan.service gate-watchdog.service; do
  echo "--- $s ---"
  systemctl cat "$s" 2>/dev/null | grep -E 'ExecStart|WorkingDirectory|Description' | head -4
  echo "  state: $(systemctl is-active $s 2>/dev/null) enabled: $(systemctl is-enabled $s 2>/dev/null)"
done
echo "=== 我们的实盘 ==="
ps -eo args | grep '[g]ate_bot' | grep '/opt/gate-signal-bot' | grep -E 'plan-loop|run --bot'
echo "=== 旧版本残留 ==="
ps -eo args | grep '[g]ate_bot' | grep -E '/root/ai trader|/root/trader bot' | head -5 || echo "  (无)"
echo "=== DONE ==="
