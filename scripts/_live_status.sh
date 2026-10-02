#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/omnialpha

echo "=== 1) 服务/进程 ==="
systemctl is-active omnialpha-watchdog
ps -eo args | grep '[g]ate_bot' | grep -cE 'plan-loop|paper-run|run --bot|supervisor'

echo "=== 2) 账户/持仓/挂单 ==="
.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, ".")
from pathlib import Path
for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from omnialpha.config import load_bot_config
cfg = load_bot_config(Path("config/bots/brooks-btc.yaml"))
c = cfg.create_client()
acct = c.get_account() or {}
print("账户 total :", acct.get("total"))
print("available :", acct.get("available"))
print("unrealised:", acct.get("unrealised_pnl"))
pos = [p for p in (c.get_positions() or []) if int(p.get("size") or 0) != 0]
print("持仓:", len(pos))
for p in pos:
    print(f"  {p.get('contract')} size={p.get('size')} entry={p.get('entry_price')} mark={p.get('mark_price')} pnl={p.get('unrealised_pnl')}")
orders = c.list_price_orders("BTC_USDT") or []
print("挂单:", len(orders))
for o in orders:
    init = o.get("initial") or {}
    trig = o.get("trigger") or {}
    print(f"  {init.get('text') or o.get('text')} size={init.get('size') or o.get('size')} trigger={trig.get('price')} status={o.get('status')} id={str(o.get('id'))[-8:]}")
PY

echo "=== 3) 最近交易日志 ==="
if [ -f data/bots/brooks-btc/logs/trades.jsonl ]; then
  tail -5 data/bots/brooks-btc/logs/trades.jsonl | .venv/bin/python -c '
import json,sys
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try: d=json.loads(line)
    except Exception: continue
    steps=d.get("steps") or []
    acts=[f"{s.get(\"action\")}:{s.get(\"symbol\")}:{\"ok\" if s.get(\"ok\") else \"fail\"}" for s in steps if isinstance(s,dict)]
    print(f"  {d.get(\"ts\",\"\")[:19]} ok={d.get(\"ok\")} {acts}")
'
else
  echo "  (无 trades.jsonl)"
fi

echo "=== 4) 最近 plan ==="
ls -t data/bots/brooks-btc/state/*.thinking.json 2>/dev/null | head -2 | while read f; do
  echo "--- $(basename "$f") ---"
  .venv/bin/python -c "
import json,sys
d=json.loads(open('$f',encoding='utf-8').read())
head=d.get('content_head') or ''
print(head[:400])
"
done

echo "=== 5) alerts.json ==="
.venv/bin/python -c '
from pathlib import Path
import json
p = Path("data/bots/brooks-btc/state/alerts.json")
if p.exists():
    d=json.loads(p.read_text(encoding="utf-8"))
    alerts=d.get("alerts") or []
    print("告警数:", len(alerts))
    for a in alerts[-5:]:
        print("  ", a.get("type"), a.get("detail"))
else:
    print("  (无告警文件)")
'

echo "=== 6) 错误日志尾 ==="
tail -5 data/bots/brooks-btc/logs/run.err 2>/dev/null | head -5
tail -3 data/bots/brooks-btc/logs/plan.err 2>/dev/null | head -3

echo "=== LIVE_DONE ==="
