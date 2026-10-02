# -*- coding: utf-8 -*-
"""三阶段讨论试跑 + 工具用量统计（防 AI 偷懒）。"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\OmniAlpha")
os.chdir(ROOT)
os.environ["OMNIALPHA_ROOT"] = str(ROOT)

sb = ROOT / "scripts" / "secrets.bat"
if sb.exists():
    for line in sb.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("if ") and "set " in line:
            kv = line.split("set ", 1)[-1].strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

print("=== 三阶段讨论试跑 ===")
print("成员: pa-a(价格行为) / smc-paper(SMC) / orderflow-paper(订单流)")
print("融合: weighted_vote + 讨论 3 轮")
print()

r = subprocess.run(
    [str(ROOT / ".venv" / "Scripts" / "python.exe"),
     "-m", "omnialpha", "--root", str(ROOT),
     "persona-run", "--group", "disc-trio", "--once"],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
)
out = r.stdout or ""
print(out[-2500:])
if r.stderr:
    err = [l for l in r.stderr.splitlines() if "ERROR" in l or "error" in l.lower()]
    if err:
        print("--- 错误 ---")
        for l in err[-5:]:
            print(" ", l[:150])
print("exit:", r.returncode)

# ── 工具用量统计 ─────────────────────────────
print("\n=== 各人格工具用量（防偷懒检查）===")
for bot in ["pa-a", "smc-paper", "orderflow-paper"]:
    st = ROOT / "data" / "bots" / bot / "state"
    files = sorted(st.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"\n--- {bot}: 无记录 ---")
        continue
    import json
    d = json.loads(files[0].read_text(encoding="utf-8"))
    usage = d.get("tool_usage") or []
    summary = d.get("tool_usage_summary") or {}
    print(f"\n--- {bot} ({files[0].name}) ---")
    print(f"  工具调用总数: {summary.get('total_calls', len(usage))}")
    print(f"  查过行情数据: {'✅' if summary.get('data_checked') else '❌ 没查数据！'}")
    counts = summary.get("counts") or {}
    if counts:
        for t, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"    {t}: {n} 次")
    else:
        print("    (无工具调用)")
    if usage:
        print("  最近调用:")
        for u in usage[-3:]:
            print(f"    {u.get('tool')} args={u.get('args')} → {str(u.get('result_preview',''))[:60]}")

print("\n=== TRIO_DONE ===")
