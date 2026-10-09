# -*- coding: utf-8 -*-
"""把每轮模型原文的 chips 打印出来（决策内容对比）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\omnialpha")
RES = Path(r"C:\Users\w6485\Desktop\测试\_ab_multi\results")


def think_raw(arm: str, t0: float, dur: float):
    bot = "ab-multi-%s" % arm
    best = None
    for p in (ROOT / "data" / "bots" / bot / "state").glob("*.thinking.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        ts = float(d.get("ts") or 0)
        if t0 - 30 <= ts <= t0 + dur + 60 and (best is None or abs(ts - t0) < abs(best[0] - t0)):
            best = (ts, d)
    if not best:
        return {}
    t = (best[1].get("content_head") or "").strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        return {}


for k in (1, 2, 3, 5):
    for arm in ("cur", "new"):
        fp = RES / ("k%d_%s_r1.json" % (k, arm))
        if not fp.is_file():
            continue
        r = json.loads(fp.read_text(encoding="utf-8"))
        d = think_raw(arm, float(r["ts"]), float(r["elapsed_sec"]))
        print("=" * 100)
        print("k=%d  arm=%s   宇宙=%s" % (k, arm, [s.split("_")[0] for s in r["symbols"]]))
        print("  plan.reasoning: %s" % d.get("reasoning"))
        for c in (d.get("chips") or []):
            print("   -- %-9s %-14s conf=%s region=%s tf=%s read=%d" % (
                c.get("symbol"), c.get("action"), c.get("confidence"),
                c.get("region"), c.get("kline_tf"), len(c.get("kline_read") or [])))
            print("      reasoning: %s" % c.get("reasoning"))
            print("      tp=%s tp2=%s sl=%s price=%s trig=%s size_usd=%s lev=%s" % (
                c.get("tp"), c.get("tp2"), c.get("sl"), c.get("price"),
                c.get("trigger_price"), c.get("size_usd"), c.get("leverage")))
            kr = c.get("kline_read") or []
            if kr:
                print("      kline_read[0]=%s" % kr[0])
                print("      kline_read[-1]=%s" % kr[-1])
