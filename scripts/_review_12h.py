#!/usr/bin/env python3
"""实盘复盘：过去 12h（martin-grid 为主）。"""
import glob
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path("/opt/omnialpha")
HOURS = 12
now = int(time.time())
since = now - HOURS * 3600


def ts(t):
    return time.strftime("%m-%d %H:%M", time.gmtime(t))


print("=" * 70)
print(f"复盘窗口：{ts(since)} → {ts(now)} UTC（{HOURS}h）")
print("=" * 70)

# ① 轮次与阶梯动作
files = [f for f in glob.glob(f"{ROOT}/data/bots/martin-grid/archive/done/*.result.json")
         if os.path.getmtime(f) >= since]
steps = Counter()
placed_tot = 0
step_vals = []
rounds = 0
for f in sorted(files, key=os.path.getmtime):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    rep = d.get("report") or d
    rounds += 1
    for s in (rep.get("steps") or []):
        steps[s.get("action")] += 1
        det = s.get("detail") or {}
        if s.get("action") == "ladder_template":
            placed_tot += len(det.get("placed") or [])
            if det.get("step_pct"):
                step_vals.append((ts(int(os.path.getmtime(f))), det["step_pct"],
                                  (det.get("dynamic") or {}).get("atr_pct")))

print(f"① 轮次：{rounds} 轮")
print(f"   阶梯补齐共挂单 {placed_tot} 次（≈ {placed_tot / max(rounds, 1):.1f}/轮）")
print(f"   动作分布：{dict(steps.most_common(6))}")
if step_vals:
    print(f"   动态档距：起 {step_vals[0][1]}% → 终 {step_vals[-1][1]}%"
          f"（区间 {min(v[1] for v in step_vals):.3f}~{max(v[1] for v in step_vals):.3f}）")
    print(f"   最近 3 轮：{step_vals[-3:]}")

# ② 成交
con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/account.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT timestamp, size, price, role, realised_pnl, fee FROM trade_history "
    "WHERE contract='ETH_USDT' AND timestamp > ? ORDER BY timestamp", (since,)).fetchall()
print(f"\n② 成交 {len(rows)} 笔")
maker = [r for r in rows if str(r[3]) == "maker"]
taker = [r for r in rows if str(r[3]) == "taker"]
print(f"   maker {len(maker)} 笔 / taker {len(taker)} 笔")
buy = sum(float(r[1]) for r in rows if float(r[1]) > 0)
sell = sum(-float(r[1]) for r in rows if float(r[1]) < 0)
fee = sum(float(r[5] or 0) for r in rows)
pnl = sum(float(r[4] or 0) for r in rows)
print(f"   买入 {buy:g} 张 / 卖出 {sell:g} 张 / 净 {buy - sell:+g} 张")
print(f"   手续费 {fee:.4f} U   记录 realised_pnl {pnl:.4f} U")
for r in rows[:12]:
    print(f"     {ts(r[0])} size={r[1]:>5} px={r[2]:>9} {r[3]:<6} pnl={r[4]}")

# ③ 持仓轨迹
print("\n③ 持仓轨迹（position_history 采样）")
ph = con.execute(
    "SELECT timestamp, mode, size, entry_price, mark_price, unrealised_pnl "
    "FROM position_history WHERE contract='ETH_USDT' AND timestamp > ? "
    "ORDER BY timestamp", (since,)).fetchall()
if ph:
    sizes = [(r[0], float(r[2] or 0)) for r in ph if str(r[1]) == "dual_long"]
    nonzero = [s for s in sizes if abs(s[1]) > 0]
    print(f"   采样 {len(sizes)} 次，非零 {len(nonzero)} 次"
          f"（{len(nonzero) / max(len(sizes), 1) * 100:.0f}% 时间有仓）")
    if nonzero:
        print(f"   最大持仓 {max(abs(s[1]) for s in sizes):g} 张")
        print(f"   末次：{ts(nonzero[-1][0])} size={nonzero[-1][1]:g}")
else:
    print("   (无记录)")

# ④ 账户权益
try:
    bal = con.execute("SELECT updated_at, total, available, unrealised_pnl "
                      "FROM balance_current WHERE id=1").fetchone()
    if bal:
        print(f"\n④ 账户：{ts(bal[0])} 总额 {bal[1]} / 可用 {bal[2]} / 未实现 {bal[3]}")
except Exception as e:  # noqa: BLE001
    print("\n④ 账户读取失败:", e)
con.close()

# ⑤ 错误
print("\n⑤ 日志错误")
import subprocess  # noqa: E402
for u in ("omnialpha-martin-grid", "omnialpha-kline-live"):
    out = subprocess.run(f"journalctl -u {u} --since '-12 hour' --no-pager | "
                         "grep -icE 'error|traceback|exception'",
                         shell=True, capture_output=True, text=True).stdout.strip()
    print(f"   {u}: {out} 行")
log = f"{ROOT}/data/bots/martin-grid/logs/plan.err"
ok = subprocess.run(f"grep -c \"'ok': True\" {log}", shell=True,
                    capture_output=True, text=True).stdout.strip()
bad = subprocess.run(f"grep -c \"'ok': False\" {log}", shell=True,
                     capture_output=True, text=True).stdout.strip()
print(f"   plan 轮次 ok={ok} fail={bad}")
