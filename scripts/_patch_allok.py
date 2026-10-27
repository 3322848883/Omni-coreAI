#!/usr/bin/env python3
"""dry-run 覆盖 indicator_nulls 之后重算 all_ok（否则 all_ok 仍是覆盖前的值）。"""
import pathlib

p = pathlib.Path("pa-data-source/migrate_v2.py")
s = p.read_text(encoding="utf-8")

old = '''                "nulls": gates["indicator_nulls"].get("nulls", 0),
            }'''
new = '''                "nulls": gates["indicator_nulls"].get("nulls", 0),
            }
            # all_ok 是 run_gates 内部算的，早于上面的覆盖 → 必须重算，
            # 否则「7 项全绿但 all_ok=False」（实测 2026-10-28）。
            gates["all_ok"] = {
                "ok": all((v or {}).get("ok") for k, v in gates.items() if k != "all_ok")
            }'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")
print("all_ok recompute added")
