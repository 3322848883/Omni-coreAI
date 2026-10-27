#!/usr/bin/env python3
"""打印 migrate_v2 dry-run 的 8 项门禁状态（只读）。"""
import json
import subprocess

out = subprocess.run(
    ["python3", "migrate_v2.py", "--db", "data/kline.db", "--dry-run"],
    cwd="/opt/omnialpha/pa-data-source", capture_output=True, text=True, timeout=280)
raw = out.stdout.strip()
try:
    d = json.loads(raw)
except Exception:
    print("STDOUT:", raw[:800])
    print("STDERR:", out.stderr[-600:])
    raise SystemExit(1)

gates = d.get("gates") or {}
print(f"=== 迁移门禁（all_ok={gates.get('all_ok', {}).get('ok')}）===")
for k, v in gates.items():
    if k == "all_ok":
        continue
    ok = (v or {}).get("ok")
    detail = json.dumps(v, ensure_ascii=False)
    print(f"  {'✓' if ok else '✗'} {k:14s} {detail[:150]}")
print("\nbackup:", json.dumps(d.get("backup"), ensure_ascii=False)[:200])
print("swapped:", d.get("swapped"), "| dry_run:", d.get("dry_run"))
