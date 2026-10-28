#!/usr/bin/env python3
"""全面复核：数据层 v2 / 服务健康 / 实盘盘面 / 日志异常。"""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
DS = ROOT / "pa-data-source"
DB = DS / "data/kline.db"


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=40).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"ERR {e}"


now = int(time.time())
print("=" * 60)
print("① 服务与引擎")
print("=" * 60)
for u in ("omnialpha-kline-live", "omnialpha-kline-testnet", "omnialpha-martin-grid",
          "omnialpha-pa-eth-v5", "omnialpha-ladder-v5-paper", "omnialpha-aux"):
    st = sh(f"systemctl is-active {u}")
    rs = sh(f"systemctl show -p NRestarts --value {u}")
    since = sh(f"systemctl show -p ActiveEnterTimestamp --value {u}")
    print(f"  {u:32s} {st:9s} restarts={rs:3s} since={since[:19]}")
pid = sh("systemctl show -p MainPID --value omnialpha-kline-live")
eng = sh(f"tr '\\0' '\\n' < /proc/{pid}/environ | grep PA_INDICATOR_ENGINE")
print(f"  引擎: {eng or '(legacy)'}")

print("\n" + "=" * 60)
print("② 数据层（v2）")
print("=" * 60)
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
print("  user_version:", con.execute("PRAGMA user_version").fetchone()[0])
print("  表:", [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")])
cols = [c for c in (r[1] for r in con.execute("PRAGMA table_info(kline)"))
        if c not in ("t", "o", "h", "l", "c", "v", "sum", "symbol", "interval", "closed")]
print(f"  指标列 {len(cols)}:", cols)
tot = con.execute("SELECT count(*) FROM kline").fetchone()[0]
print(f"  总行数: {tot}")
print("\n  近 8h 新增（按 src）：")
for src, n in con.execute("SELECT src, count(*) FROM kline WHERE t > ? GROUP BY src",
                          (now - 8 * 3600,)):
    print(f"    src={src!r}: {n}")

print("\n  最近 8h 收盘 bar 的指标 NULL（应为 0）：")
batch = " OR ".join(f"[{c}] IS NULL" for c in cols)
n_null = con.execute(f"SELECT count(*) FROM kline WHERE t > ? AND closed=1 AND ({batch})",
                     (now - 8 * 3600,)).fetchone()[0]
n_rows = con.execute("SELECT count(*) FROM kline WHERE t > ? AND closed=1",
                     (now - 8 * 3600,)).fetchone()[0]
print(f"    {n_null} / {n_rows} 行有 NULL")

print("\n  各序列最新 bar 时间（滞后检查）：")
lag = []
for sym, iv, mx, cnt in con.execute(
        "SELECT symbol, interval, max(t), count(*) FROM kline GROUP BY symbol, interval"):
    mins = (now - mx) / 60
    lag.append((mins, sym, iv, cnt))
for mins, sym, iv, cnt in sorted(lag, reverse=True)[:6]:
    print(f"    {sym:10s} {iv:4s} 滞后 {mins:6.1f} 分钟  行数={cnt}")

print("\n  integrity:", con.execute("PRAGMA integrity_check").fetchone()[0])
con.close()
print("  库大小:", sh(f"du -h {DB} | cut -f1"), "| backup:",
      sh(f"ls -la {DS}/data/kline_v1_20261027.db 2>/dev/null | awk '{{print $5}}'"))

print("\n" + "=" * 60)
print("③ 实盘盘面（martin-grid）")
print("=" * 60)
sys.path.insert(0, str(ROOT))
from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()
for p in c.get_positions():
    if p.get("contract") == "ETH_USDT" and float(p.get("size") or 0) != 0:
        print(f"  持仓 {p.get('mode')} size={p.get('size')} entry={p.get('entry_price')}")
orders = c.list_orders("ETH_USDT")
zero = [o for o in orders if float(o.get("size") or 0) == 0]
entry = [o for o in orders if float(o.get("size") or 0) > 0]
print(f"  入场挂单 {len(entry)} 条 | size=0 {len(zero)} 条")
for o in sorted(entry, key=lambda x: -float(x.get("price") or 0)):
    print(f"    price={o.get('price'):>8s} size={o.get('size'):>3} text={o.get('text')}")
for o in c.list_price_orders("ETH_USDT"):
    i = o.get("initial") or {}
    print(f"  触发单 trigger={(o.get('trigger') or {}).get('price')} size={i.get('size')}")

print("\n" + "=" * 60)
print("④ 日志异常（近 8h）")
print("=" * 60)
for u in ("omnialpha-kline-live", "omnialpha-martin-grid", "omnialpha-pa-eth-v5"):
    e = sh(f"journalctl -u {u} --since '-8 hour' --no-pager | grep -icE 'error|traceback|exception'")
    print(f"  {u:32s} error 行数={e}")
for b in ("martin-grid", "pa-eth-v5"):
    ok = sh(f"grep \"'ok': True\" {ROOT}/data/bots/{b}/logs/plan.err | wc -l")
    last = sh(f"grep plan {ROOT}/data/bots/{b}/logs/plan.err | tail -1 | cut -c1-95")
    bad = sh(f"grep -c \"'ok': False\" {ROOT}/data/bots/{b}/logs/plan.err")
    print(f"  {b}: ok轮次={ok} 失败轮次={bad}\n     最近: {last}")
