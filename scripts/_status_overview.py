# -*- coding: utf-8 -*-
"""实盘 + 模拟盘运行概况。"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(r"C:\Users\w6485\Desktop\测试\gate-signal-bot")


def cmd(*args):
    r = subprocess.run([sys.executable, "-m", "gate_bot", *args],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    return r.stdout


# ── 心跳 ──────────────────────────────────────────
d = json.loads(cmd("status"))
print("== 心跳 ==")
for h in (d.get("heartbeats") or []):
    print(f"  {h['bot_id']}/{h['component']:<10} {h['detail']:<8} age={h['age_s']:.0f}s pid={h['pid']}")

print()
print("== 启用 bot ==")
for b in d.get("bots", []):
    if b.get("enabled"):
        print(f"  {b['bot_id']:<20} env={b['env']:<6} done={b['done']:<5} failed={b['failed']}")

# ── 实盘持仓/挂单 ────────────────────────────────
print()
print("== 实盘 trades 尾 ==")
print(cmd("trades", "--bot", "brooks-btc", "--tail", "5")[:1500])
