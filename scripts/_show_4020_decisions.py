#!/usr/bin/env python3
"""19:29 轮完整决策链 + 每腿执行结果（模型给了什么、引擎怎么处理）。"""
import glob
import json
import os

A = glob.glob("/opt/omnialpha/data/bots/martin-grid/archive/done/*4017*") + \
    glob.glob("/opt/omnialpha/data/bots/martin-grid/archive/done/*4020*")
A = [f for f in A if not f.endswith(".error.json")]
print("files:", [os.path.basename(f) for f in A])

# 计划 chips
plans = [f for f in A if not f.endswith(".result.json")]
if plans:
    d = json.load(open(sorted(plans)[-1], encoding="utf-8"))
    orders = d.get("orders") or d.get("chips") or []
    print(f"\n=== 模型计划 {len(orders)} 条 ===")
    for o in orders:
        if isinstance(o, dict):
            print(f"  {str(o.get('action')):14s} price={o.get('price')} size={o.get('size')} "
                  f"size_usd={o.get('size_usd')} sl={o.get('sl')} side={o.get('side')}")

# 执行结果
res = [f for f in A if f.endswith(".result.json")]
if res:
    d = json.load(open(sorted(res)[-1], encoding="utf-8"))
    rep = d.get("report") or d
    print(f"\n=== 执行结果 ok={rep.get('ok')} ===")
    for st in (rep.get("steps") or [])[:14]:
        err = st.get("error") or ""
        det = st.get("detail") or {}
        extra = ""
        if det.get("size_error"):
            extra = f" size_error={det['size_error']}"
        elif det.get("contracts") is not None:
            extra = f" contracts={det['contracts']}"
        if err or extra:
            print(f"  {st.get('action'):14s} ok={st.get('ok')} {str(err)[:110]}{extra}")
