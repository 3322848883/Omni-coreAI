# -*- coding: utf-8 -*-
"""列 paper bot 的 strategist 分析间隔（区分 poll_interval_sec）。"""
import re
from pathlib import Path

for p in sorted(Path("config/bots").glob("*.yaml")):
    t = p.read_text(encoding="utf-8")
    if "env: paper" not in t:
        continue
    bid = re.search(r"bot_id:\s*(\S+)", t)
    # strategist 段内 interval_sec
    m = re.search(r"strategist:\s*\n(.*)$", t, re.S)
    seg = m.group(1) if m else t
    iv = re.search(r"^\s+interval_sec:\s*(\S+)", seg, re.M)
    tf = re.search(r"^\s+timeframe:\s*(\S+)", seg, re.M)
    ev = re.search(r"^\s+event_timeframe:\s*(\S+)", seg, re.M)
    eon = re.search(r"^\s+event_on_kline_close:\s*(\S+)", seg, re.M)
    name = bid.group(1) if bid else p.stem
    sec = int(iv.group(1)) if iv else 0
    human = f"{sec//3600}h" if sec >= 3600 else f"{sec//60}m" if sec >= 60 else f"{sec}s"
    print(
        f"{name:22} 定时={human:4}  主周期={tf.group(1) if tf else '-':4} "
        f"K线收盘触发={eon.group(1) if eon else '-':5} 触发周期={ev.group(1) if ev else '-'}"
    )
