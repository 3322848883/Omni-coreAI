#!/usr/bin/env python3
"""诊断 ladder-v5-paper 的 'size required'：看失败轮的 chips 与字段。"""
import glob
import json
import os

BOT = "/opt/omnialpha/data/bots/ladder-v5-paper"
target = "20261027-131152"
cands = []
for pat in ("state/*.json", "archive/*/*.json", "archive/*.json", "*.json"):
    cands.extend(glob.glob(os.path.join(BOT, pat)))
hit = [p for p in cands if target in os.path.basename(p)]
print("匹配文件:", hit[:3])

for p in hit[:1]:
    d = json.load(open(p, encoding="utf-8"))
    print("keys:", list(d.keys())[:12])
    chips = d.get("chips") or []
    print(f"chips={len(chips)}")
    for c in chips[:12]:
        meta = c.get("meta") or {}
        print(" ", {k: c.get(k) for k in ("action", "symbol", "side", "price", "size", "size_usd", "size_pct")},
              "| meta.size=", meta.get("size"), "meta.qty=", meta.get("qty"),
              "| type=", meta.get("type"))
