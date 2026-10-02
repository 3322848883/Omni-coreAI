#!/bin/bash
cd /opt/omnialpha
echo "=== 核心功能文件 ==="
for f in \
  omnialpha/watchdog.py \
  omnialpha/monitoring/alerts.py \
  omnialpha/monitoring/notify.py \
  omnialpha/pidlock.py \
  omnialpha/backtest/__init__.py \
  omnialpha/strategist/tv_indicators/__init__.py \
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
.venv/bin/python -m omnialpha --help 2>&1 | grep -E '^\s+(watchdog|backtest|paper-run|persona-run|supervisor|migrate)' || true

echo "=== 关键代码特性 ==="
.venv/bin/python -c '
import re
checks = [
  ("PidLock OS 锁", "omnialpha/pidlock.py", "msvcrt|fcntl"),
  ("日初权益隔离", "omnialpha/executor.py", "_account_risk"),
  ("告警落盘", "omnialpha/executor.py", "alert_store"),
  ("波动率仓位", "omnialpha/executor.py", "vol_adjust_size"),
  ("成交卡片", "omnialpha/monitoring/notify.py", "format_trade_card"),
  ("看门狗 enabled 开关", "omnialpha/watchdog.py", "enabled"),
]
for name, path, pat in checks:
    s = open(path, encoding="utf-8").read()
    print("  OK      " if re.search(pat, s) else "  MISSING ", name)
'

echo "=== 测试总数 ==="
.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E 'Ran |OK$|FAILED' | tail -2

echo "=== COMPLETENESS_DONE ==="
