#!/usr/bin/env python3
"""最近 N 轮动态档距轨迹（step / atr% / 带宽% / placed / protection）。"""
import glob
import json
import os
import time

files = sorted(glob.glob("/opt/omnialpha/data/bots/martin-grid/archive/done/*.result.json"),
               key=os.path.getmtime)[-8:]
print(f"{'时间':>6} {'step%':>7} {'atr%':>7} {'bw%':>7} {'raw%':>7} {'placed':>7} {'prot':>5}")
for f in files:
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    rep = d.get("report") or d
    ts = time.strftime("%H:%M", time.localtime(os.path.getmtime(f)))
    for s in (rep.get("steps") or []):
        if s.get("action") != "ladder_template":
            continue
        det = s.get("detail") or {}
        dyn = det.get("dynamic") or {}
        print(f"{ts:>6} {str(det.get('step_pct')):>7} {str(dyn.get('atr_pct')):>7} "
              f"{str(dyn.get('bw_pct')):>7} {str(dyn.get('raw_step_pct')):>7} "
              f"{len(det.get('placed') or []):>7} "
              f"{'有' if det.get('protection') else '—':>5}")
