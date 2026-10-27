#!/usr/bin/env python3
"""打印 migrate_v2 实际使用的 specs（哪些指标/列/参数）。"""
import json
import sys

sys.path.insert(0, ".")
import migrate_v2 as m  # noqa: E402
import indicator_config as ic  # noqa: E402

# 复现 migrate_v2 里 specs 的来源
print("=== indicator_config 公开函数 ===")
print([n for n in dir(ic) if not n.startswith("_")])

try:
    specs = ic.load_specs()
    print(f"\nload_specs() → {len(specs)} 条")
    for s in specs:
        print("  ", getattr(s, "name", "?"), "| kind=", getattr(s, "kind", "?"),
              "| params=", getattr(s, "params", "?"),
              "| columns=", list(getattr(s, "columns", [])))
except Exception as e:  # noqa: BLE001
    print("load_specs 失败:", type(e).__name__, e)

try:
    print("\nSUPPORTED_KINDS:", ic.SUPPORTED_KINDS)
except Exception as e:  # noqa: BLE001
    print("无 SUPPORTED_KINDS:", e)
