#!/usr/bin/env python3
"""深挖：轮次频率 / 账户权益历史 / 为何阶梯 0 挂单 / 那条 error。"""
import glob
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
now = int(time.time())
since = now - 12 * 3600

print("① plan 轮次频率（plan.err 时间戳，近 12h）")
out = subprocess.run(
    f"grep -o '2026-10-2[89] [0-9][0-9]:[0-9][0-9]' {ROOT}/data/bots/martin-grid/logs/plan.err "
    "| sort -u | tail -60", shell=True, capture_output=True, text=True).stdout.strip()
times = out.split("\n") if out else []
recent = [t for t in times if t >= time.strftime("%m-%d %H:%M", time.gmtime(since))]
print(f"   不同分钟数：{len(recent)}（≈ 轮次数）")
if recent:
    print(f"   最早 {recent[0]}  最晚 {recent[-1]}")
    print(f"   尾部：{recent[-8:]}")

print("\n② account.db 表清单")
con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/account.db?mode=ro", uri=True)
print("   ", [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])

print("\n③ 近 12h 成交汇总（按 role）")
rows = con.execute(
    "SELECT timestamp, size, price, role, fee FROM trade_history "
    "WHERE contract='ETH_USDT' AND timestamp > ? ORDER BY timestamp", (since,)).fetchall()
for r in rows:
    print(f"   {time.strftime('%H:%M', time.gmtime(r[0]))} {r[1]:>5} @ {r[2]:>9} {r[3]:<6} fee={r[4]}")

print("\n④ 是否有权益历史表")
for t in ("balance_history", "equity_history", "account_snapshots"):
    try:
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        last = con.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 1").fetchone()
        print(f"   {t}: {n} 行, last={last}")
    except Exception:  # noqa: BLE001
        pass
con.close()

print("\n⑤ 近 12h 每轮 ladder_template 明细（从 result.json 全量扫）")
files = sorted(glob.glob(f"{ROOT}/data/bots/martin-grid/archive/done/*.result.json"),
               key=os.path.getmtime)
hit = 0
for f in files:
    if os.path.getmtime(f) < since:
        continue
    hit += 1
    d = json.load(open(f, encoding="utf-8"))
    rep = d.get("report") or d
    steps = [(s.get("action"), (s.get("detail") or {})) for s in (rep.get("steps") or [])]
    ts = time.strftime("%H:%M", time.gmtime(os.path.getmtime(f)))
    desc = []
    for a, det in steps:
        if a == "ladder_template":
            desc.append(f"tpl(placed={len(det.get('placed') or [])},"
                        f"skip={det.get('skipped')})")
        elif a in ("modify_tp_sl", "reduce_long", "cancel_all"):
            desc.append(a)
    print(f"   {ts} {' | '.join(desc) or '(无操作)'}")
print(f"   共 {hit} 个 result 文件")

print("\n⑥ kline-live 的 error 行")
print(subprocess.run(
    "journalctl -u omnialpha-kline-live --since '-12 hour' --no-pager "
    "| grep -iE 'error|traceback|exception' | tail -3",
    shell=True, capture_output=True, text=True).stdout.strip() or "   (无)")
