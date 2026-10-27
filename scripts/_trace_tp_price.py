#!/usr/bin/env python3
"""核实 TP 2364.5 的选择依据：在最近几轮 reasoning 里找 2364.5 的出处。"""
import glob
import json
import os
import re

BOT = "/opt/omnialpha/data/bots/martin-grid"
files = sorted(glob.glob(f"{BOT}/state/*thinking.json") +
               glob.glob(f"{BOT}/archive/*/*.json"), key=os.path.getmtime)
files = [f for f in files if os.path.getmtime(f) < 1793116000]  # 只看 TP 首挂之前的轮次
print(f"扫描 {len(files)} 个文件，找 2364.5 的上下文：\n")
shown = 0
for f in reversed(files):   # 从新往旧找第一次给出 2364.5 依据的轮次
    try:
        blob = open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    if "2364.5" not in blob and "2364" not in blob:
        continue
    print(f"--- {os.path.basename(f)} ---")
    for m in re.finditer(r".{0,110}2364\.5.{0,150}", blob):
        print("  ·", m.group(0)[:260].replace("\\n", " "))
        print()
        shown += 1
        if shown >= 6:
            break
    if shown >= 6:
        break
