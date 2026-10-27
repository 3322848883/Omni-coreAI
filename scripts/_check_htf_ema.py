#!/usr/bin/env python3
"""验证 d66039d（HTF 预热）是否生效：看 pa-eth-v5 最近几轮 reasoning 里是否还报「缺 EMA」。"""
import glob
import json
import os
import re

BOT = "/opt/omnialpha/data/bots/pa-eth-v5"
files = sorted(glob.glob(f"{BOT}/state/*.json") + glob.glob(f"{BOT}/archive/*/*.json"),
               key=os.path.getmtime)[-12:]
print(f"最近 {len(files)} 个计划文件：\n")
for f in files[-6:]:
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    r = d.get("reasoning") or ""
    if not r and isinstance(d.get("meta"), dict):
        r = d["meta"].get("plan_reasoning") or ""
    r = re.sub(r"\s+", " ", r)[:300]
    print(f"{os.path.basename(f)[:34]:36s} {r}")
    if r:
        print(f"{'':36s} → 提到 EMA={'EMA' in r or 'ema' in r} | 报缺={'缺' in r or 'None' in r or 'null' in r}\n")
