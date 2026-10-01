# -*- coding: utf-8 -*-
"""实盘持仓 / 挂单 / 保护单。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GATE_BOT_ROOT", r"C:\Users\w6485\Desktop\测试\gate-signal-bot")
from gate_bot.config import load_bot_config  # noqa: E402

cfg = load_bot_config(Path(r"C:\Users\w6485\Desktop\测试\gate-signal-bot\config\bots\brooks-btc.yaml"))
client = cfg.create_client()
acct = client.get_account() or {}
print("== 账户 ==")
print(f"  total={acct.get('total')}  avail={acct.get('available')}  unreal={acct.get('unrealised_pnl')}")

print()
print("== 持仓 ==")
for p in (client.get_positions() or []):
    sz = int(p.get("size") or 0)
    if sz == 0:
        continue
    print(f"  {p.get('contract')} size={sz} entry={p.get('entry_price')} mark={p.get('mark_price')} pnl={p.get('unrealised_pnl')}")

print()
print("== 挂单 ==")
for o in (client.list_price_orders("BTC_USDT") or []):
    st = str(o.get("status") or "").lower()
    if st in ("finished", "cancelled", "filled", "triggered", "failed", "closed"):
        continue
    init = o.get("initial") or {}
    trig = o.get("trigger") or {}
    print(f"  id={str(o.get('id'))[-8:]} text={init.get('text') or o.get('text')} "
          f"size={init.get('size') or o.get('size')} price={init.get('price') or o.get('price')} "
          f"trigger={trig.get('price')} status={o.get('status')}")

print()
print("== alerts.json ==")
from gate_bot.monitoring import read_alerts  # noqa: E402

root = Path(r"C:\Users\w6485\Desktop\测试\gate-signal-bot")
rows = read_alerts(root, "brooks-btc")
if not rows:
    print("  (无告警)")
for r in rows[-5:]:
    print(f"  [{r.get('type')}] {r.get('detail')}")
