#!/bin/bash
for s in OmniAlpha.service OmniAlpha-ws.service OmniAlpha-ws-plan.service omnialpha-watchdog.service; do
  echo "--- $s ---"
  systemctl cat "$s" 2>/dev/null | grep -E 'ExecStart|WorkingDirectory|Description' | head -4
  echo "  state: $(systemctl is-active $s 2>/dev/null) enabled: $(systemctl is-enabled $s 2>/dev/null)"
done
echo "=== 我们的实盘 ==="
ps -eo args | grep '[g]ate_bot' | grep '/opt/omnialpha' | grep -E 'plan-loop|run --bot'
echo "=== 旧版本残留 ==="
ps -eo args | grep '[g]ate_bot' | grep -E '/root/ai trader|/root/trader bot' | head -5 || echo "  (无)"
echo "=== DONE ==="
