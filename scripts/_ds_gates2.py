#!/usr/bin/env python3
"""跑补丁版 migrate_v2 的 dry-run 并打印门禁（只读）。"""
import json
import subprocess

out = subprocess.run(
    ["python3", "_migrate_v2_patched.py", "--db", "data/kline.db", "--dry-run"],
    cwd="/opt/omnialpha/pa-data-source", capture_output=True, text=True, timeout=280)
try:
    d = json.loads(out.stdout)
except Exception:
    print("STDOUT:", out.stdout[:600])
    print("STDERR:", out.stderr[-800:])
    raise SystemExit(1)

g = d.get("gates") or {}
print("all_ok =", g.get("all_ok", {}).get("ok"))
for k, v in g.items():
    if k == "all_ok":
        continue
    print(f"  {'✓' if (v or {}).get('ok') else '✗'} {k:16s} {json.dumps(v, ensure_ascii=False)[:120]}")
print("swapped =", d.get("swapped"))
