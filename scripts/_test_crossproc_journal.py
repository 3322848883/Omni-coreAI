"""跨进程并发写 journal 测试。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
td = Path(tempfile.mkdtemp())

WORKER = f'''
import sys
from pathlib import Path
sys.path.insert(0, r"{ROOT}")
from gate_bot.skillkit.journal import append_journal
root = Path(sys.argv[1])
n = int(sys.argv[2])
for i in range(n):
    append_journal(root, {{"kind": "proc", "skill_id": "x", "i": i}})
'''
script = td / "worker.py"
script.write_text(WORKER, encoding="utf-8")

N_PROC = 4
N_EACH = 50
procs = [subprocess.Popen([sys.executable, str(script), str(td), str(N_EACH)]) for _ in range(N_PROC)]
for p in procs:
    p.wait()

jp = td / "logs" / "skill_journal.jsonl"
lines = jp.read_text(encoding="utf-8").strip().splitlines()
bad = 0
for ln in lines:
    try:
        json.loads(ln)
    except Exception:
        bad += 1

print(f"{N_PROC} 进程 x {N_EACH} = 期望 {N_PROC * N_EACH} 行")
print(f"实际行数: {len(lines)}   损坏行: {bad}")
print("结论:", "PASS 跨进程安全" if len(lines) == N_PROC * N_EACH and bad == 0 else "FAIL 跨进程丢写/损坏")
