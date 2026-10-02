# -*- coding: utf-8 -*-
"""失败单 / 异常 / 告警 汇总。"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\OmniAlpha")


def top_errors(label, rows, n=8):
    print(f"\n== {label}（共 {len(rows)}）==")
    if not rows:
        print("  (无)")
        return
    c = Counter(rows)
    for msg, cnt in c.most_common(n):
        print(f"  {cnt:4d}×  {msg[:110]}")


# ── 1. failed archive 的 error 原因 ─────────────────
print("==" + "=" * 60)
print("1) 失败信号归档（archive/failed/*.error.json）")
print("==" + "=" * 60)
errs = []
per_bot = Counter()
for p in ROOT.glob("data/bots/*/archive/failed/*.error.json"):
    bot = p.parts[p.parts.index("bots") + 1]
    per_bot[bot] += 1
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        e = d.get("error") or d.get("reason") or d.get("detail") or str(d)[:120]
        errs.append(f"{bot}: {str(e)[:100]}")
    except Exception as ex:  # noqa: BLE001
        errs.append(f"{bot}: <read fail {ex}>")

print(f"\n按 bot：{dict(per_bot.most_common())}")
top_errors("失败原因 Top", errs, 12)

# ── 2. 每 bot failed 计数 vs done ───────────────────
print()
print("==" + "=" * 60)
print("2) 每 bot failed 明细（对比 done）")
print("==" + "=" * 60)
try:
    import subprocess, sys

    r = subprocess.run([sys.executable, "-m", "omnialpha", "status"],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    d = json.loads(r.stdout)
    rows = []
    for b in d.get("bots", []):
        if not b.get("enabled"):
            continue
        if b.get("failed", 0) > 0:
            rows.append((b["bot_id"], b["done"], b["failed"]))
    rows.sort(key=lambda x: -x[2])
    for bid, done, failed in rows:
        rate = failed / max(done + failed, 1) * 100
        print(f"  {bid:<20} done={done:<5} failed={failed:<5} fail_rate={rate:.0f}%")
except Exception as ex:  # noqa: BLE001
    print("  status 读取失败:", ex)

# ── 3. 日志中的 error/warning ───────────────────────
print()
print("==" + "=" * 60)
print("3) 进程日志 error/exception（每 bot 最近 5 条）")
print("==" + "=" * 60)
log_hits = []
for p in ROOT.glob("data/bots/*/logs/*.err"):
    bot = p.parts[p.parts.index("bots") + 1]
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        hits = [ln for ln in lines if any(k in ln.lower() for k in
                                          ("error", "exception", "traceback", "critical", "fatal"))]
        if hits:
            log_hits.append((bot, p.name, hits[-5:]))
    except Exception:  # noqa: BLE001
        pass

if not log_hits:
    print("  (无 .err 错误)")
for bot, name, hits in log_hits[:10]:
    print(f"\n  [{bot}/{name}]")
    for h in hits:
        print(f"    {h[:120]}")

# ── 4. alerts.json ─────────────────────────────────
print()
print("==" + "=" * 60)
print("4) alerts.json 告警")
print("==" + "=" * 60)
found = False
for p in ROOT.glob("data/bots/*/state/alerts.json"):
    bot = p.parts[p.parts.index("bots") + 1]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        alerts = d.get("alerts") or []
        if alerts:
            found = True
            print(f"\n  [{bot}] {len(alerts)} 条")
            for a in alerts[-5:]:
                print(f"    {a.get('type')}: {a.get('detail')}")
    except Exception as ex:  # noqa: BLE001
        print(f"  [{bot}] 读取失败 {ex}")
if not found:
    print("  (所有 bot 无告警)")

print()
print("DONE")
