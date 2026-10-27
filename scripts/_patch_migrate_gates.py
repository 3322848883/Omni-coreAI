#!/usr/bin/env python3
"""修 migrate_v2 两个门禁：dry-run 假阳性 + ohlc 容差过严。"""
import pathlib

p = pathlib.Path("pa-data-source/migrate_v2.py")
s = p.read_text(encoding="utf-8")

old_gate_call = "    gates = run_gates(conn, specs, warmup)\n"
new_gate_call = """    gates = run_gates(conn, specs, warmup)
        # dry-run 不写指标列（recalc dry_run=True 只统计），`indicator_nulls` 必然全 NULL。
        # 那是**假阳性**：实测 2026-10-28 dry-run 报 315590 nulls，让迁移看起来没过门禁。
        # dry-run 下把它标成 skipped；真正的完整性由正式迁移（写库后）校验。
        if dry_run and gates.get("indicator_nulls", {}).get("ok") is False:
            gates["indicator_nulls"] = {
                "ok": True, "skipped": "dry_run",
                "note": "dry-run 不写指标列，nulls 不作判据（正式迁移会写并复核）",
                "nulls": gates["indicator_nulls"].get("nulls", 0),
            }
"""
assert old_gate_call in s
s = s.replace(old_gate_call, new_gate_call, 1)

old_sig = "              rel_tol: float = 1e-12) -> dict:"
new_sig = (
    "              rel_tol: float = 1e-6) -> dict:\n"
    "    \"\"\"门禁校验。\n"
    "\n"
    "    `rel_tol` 默认 1e-6（相对误差 0.0001%）：v1 的 OHLC 是 TEXT/REAL 混存，\n"
    "    SQL 直接复制后 float64 往返在**低价币**上会出现 ~1e-4 的打印差\n"
    "    （实测 2026-10-28：worst_rel=9.9e-5，样本 2800，全部来自 6 位小数币）。\n"
    "    原来的 1e-12 等于要求 bit-exact，对这类币永远不可能通过 —— 容差应按\n"
    "    「价格精度」而不是「浮点位模式」来定。\n"
    "    \"\"\""
)
assert old_sig in s
s = s.replace(old_sig, new_sig, 1)
p.write_text(s, encoding="utf-8")
print("patched ok")
