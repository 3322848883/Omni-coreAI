# -*- coding: utf-8 -*-
"""三阶段讨论单次试跑：价格行为 + SMC + 订单流。"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\OmniAlpha")
os.chdir(ROOT)
os.environ["OMNIALPHA_ROOT"] = str(ROOT)

# 密钥
sb = ROOT / "scripts" / "secrets.bat"
if sb.exists():
    for line in sb.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("if ") and "set " in line:
            kv = line.split("set ", 1)[-1].strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

print("=== 三阶段讨论单次试跑 ===")
print("成员: pa-a(价格行为/brooks) / smc-paper(SMC) / orderflow-paper(订单流)")
print("融合: weighted_vote + 讨论 3 轮（反驳→深化→定稿）")
print("模型:", os.environ.get("OPENAI_BASE_URL"))
print()

r = subprocess.run(
    [str(ROOT / ".venv" / "Scripts" / "python.exe"),
     "-m", "omnialpha", "--root", str(ROOT),
     "persona-run", "--group", "disc-trio", "--once"],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
)
print(r.stdout[-3000:] if r.stdout else "(无输出)")
if r.stderr:
    print("--- stderr ---")
    print(r.stderr[-1500:])
print("exit:", r.returncode)

print("\n=== persona_log 尾部 ===")
p = ROOT / "data" / "shared" / "persona_log.jsonl"
if p.exists():
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    for ln in lines[-6:]:
        print(" ", ln[:300])
