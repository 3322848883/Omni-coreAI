#!/usr/bin/env python3
"""bot 重启后自检：unit 状态 / 最近一轮 plan 结果 / 是否有 traceback。"""
import glob
import os
import re
import subprocess
import time

BOTS = ["pa-eth-v5", "ladder-v5-paper", "martin-grid"]
now = time.time()

for bot in BOTS:
    unit = f"omnialpha-{bot}"
    act = subprocess.run(["systemctl", "is-active", unit],
                         capture_output=True, text=True).stdout.strip()
    started = subprocess.run(
        ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit],
        capture_output=True, text=True).stdout.strip()
    print(f"\n=== {bot} ===  unit={act}  since={started}")

    for name in ("plan.err", "run.err"):
        path = f"/opt/omnialpha/data/bots/{bot}/logs/{name}"
        if not os.path.exists(path):
            print(f"  {name}: (missing)")
            continue
        tail = subprocess.run(["tail", "-n", "400", path], capture_output=True,
                              text=True).stdout.splitlines()
        oks = [ln for ln in tail if "'ok': True" in ln]
        errs = [ln for ln in tail if "Traceback" in ln or "ERROR" in ln or "CRITICAL" in ln]
        print(f"  {name}: last_ok={oks[-1][:130] if oks else 'NONE'}")
        if errs:
            print(f"    recent_err={errs[-1][:140]}")
        if oks:
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", oks[-1])
            if m:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                print(f"    ok 距今 {(now - ts)/60:.1f} 分钟")
