#!/bin/bash
cd /opt/gate-signal-bot
echo "=== 核心功能文件 ==="
for f in \
  gate_bot/watchdog.py \
  gate_bot/monitoring/alerts.py \
  gate_bot/monitoring/notify.py \
  gate_bot/pidlock.py \
  gate_bot/backtest/__init__.py \
  gate_bot/strategist/tv_indicators/__init__.py \
  docs/DEPLOY-SERVER.md \
  docs/OPERATIONS.md \
  tests/test_day_start_equity.py \
  tests/test_watchdog.py \
  tests/test_alerts.py \
  tests/test_trade_alerts.py \
  tests/test_vol_sizing.py \
  tests/test_runtime.py
do
  if [ -f "$f" ]; then echo "  OK       $f"; else echo "  MISSING  $f"; fi
done

echo "=== CLI 子命令 ==="
.venv/bin/python -m gate_bot --help 2>&1 | grep -E '^\s+(watchdog|backtest|paper-run|persona-run|supervisor|migrate)' || true

echo "=== 关键代码特性 ==="
.venv/bin/python -c '
import re
checks = [
  ("PidLock OS 锁", "gate_bot/pidlock.py", "msvcrt|fcntl"),
  ("日初权益隔离", "gate_bot/executor.py", "_account_risk"),
  ("告警落盘", "gate_bot/executor.py", "alert_store"),
  ("波动率仓位", "gate_bot/executor.py", "vol_adjust_size"),
  ("成交卡片", "gate_bot/monitoring/notify.py", "format_trade_card"),
  ("看门狗 enabled 开关", "gate_bot/watchdog.py", "enabled"),
]
for name, path, pat in checks:
    s = open(path, encoding="utf-8").read()
    print("  OK      " if re.search(pat, s) else "  MISSING ", name)
'

echo "=== 测试总数 ==="
.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E 'Ran |OK$|FAILED' | tail -2

echo "=== COMPLETENESS_DONE ==="
