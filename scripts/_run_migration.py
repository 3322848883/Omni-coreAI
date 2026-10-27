#!/usr/bin/env python3
"""正式迁移 v1 → v2（使用带 3 处门禁修复的脚本副本），并打印完整结果。"""
import json
import subprocess

print("=== 迁移前 ===")
q = ("select name from sqlite_master where type='table' order by name")
out = subprocess.run(["python3", "-c",
                      f"import sqlite3;c=sqlite3.connect('data/kline.db');"
                      f"print([r[0] for r in c.execute(\"{q}\")]);"
                      f"print('rows=',c.execute('select count(*) from kline').fetchone()[0])"],
                     cwd="/opt/omnialpha/pa-data-source", capture_output=True, text=True)
print(out.stdout.strip() or out.stderr[:300])

print("\n=== 执行迁移 ===")
r = subprocess.run(["python3", "_migrate_v2_patched.py", "--db", "data/kline.db"],
                   cwd="/opt/omnialpha/pa-data-source", capture_output=True, text=True,
                   timeout=900)
try:
    d = json.loads(r.stdout)
except Exception:
    print("STDOUT:", r.stdout[:2000])
    print("STDERR:", r.stderr[-1500:])
    raise SystemExit(1)

print("swapped =", d.get("swapped"))
print("backup  =", json.dumps(d.get("backup"), ensure_ascii=False))
g = d.get("gates") or {}
print("all_ok  =", g.get("all_ok", {}).get("ok"))
for k, v in g.items():
    if k == "all_ok":
        continue
    print(f"  {'✓' if (v or {}).get('ok') else '✗'} {k:16s} {json.dumps(v, ensure_ascii=False)[:110]}")
rec = d.get("recalc") or []
print(f"recalc 序列 {len(rec)} 条，异常 {sum(1 for x in rec if x.get('missing'))} 条")
