#!/usr/bin/env python3
"""验证 BB 是否真的进了 martin-grid 的生效配置与 prompt。"""
import glob
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
mk = (bot.strategist or {}).get("market") or {}
print("effective market.indicators =", mk.get("indicators"))
print("effective mode =", mk.get("mode"), "| timeframes =", mk.get("timeframes"))

# prompt 侧证据：thinking json 的 content_head / charts 可能带快照片段；
# 直接全仓找 boll20 出现在哪个产物里
hits = []
for pat in ("data/bots/martin-grid/state/*.json",
            "data/bots/martin-grid/state/*.jsonl",
            "data/bots/martin-grid/*.jsonl"):
    for f in glob.glob(str(ROOT / pat)):
        try:
            txt = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        n = txt.count("boll20")
        if n:
            hits.append((f, n))
for f, n in sorted(hits, key=lambda x: -x[1])[:6]:
    print(f"boll20 × {n:5d}  {f}")
if not hits:
    print("（所有状态产物里都没有 boll20 —— 需要看 prompt 本体）")
