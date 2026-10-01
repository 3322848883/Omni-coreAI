# -*- coding: utf-8 -*-
"""跑 disc-trio 并输出讨论过程（反驳/改口）。"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\gate-signal-bot")
os.chdir(ROOT)
os.environ["GATE_BOT_ROOT"] = str(ROOT)
sb = ROOT / "scripts" / "secrets.bat"
if sb.exists():
    for line in sb.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("if ") and "set " in line:
            kv = line.split("set ", 1)[-1].strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT))
from gate_bot.config import load_all_bots  # noqa: E402
from gate_bot.watcher import ProjectPaths  # noqa: E402
from gate_bot.persona.config import load_persona_groups  # noqa: E402
from gate_bot.persona.runner import PersonaRunner  # noqa: E402
import gate_bot.__main__ as m  # noqa: E402

paths = ProjectPaths(ROOT)
bots = load_all_bots(ROOT / "config" / "bots")
groups = load_persona_groups(ROOT / "config" / "persona_groups.yaml")
g = next((x for x in groups if x.name == "disc-trio"), None)
print("组:", g.name, "成员:", g.members, "轮次:", g.discussion.rounds,
      "早退:", g.discussion.early_exit_on_agreement)

runners = {}
for bid in g.members:
    runners[bid] = m._build_plan_runner(bots[bid], paths)

r = PersonaRunner(ROOT, g, bots, runners)
res = r.run_once(trigger="manual")

print("\n=== 最终结果 ===")
print("决策:", res.get("decision"), "| 动作:", res.get("action"))
print("融合:", json.dumps(res.get("fusion"), ensure_ascii=False))

print("\n=== 讨论过程 ===")
for rec in res.get("discussion_log") or []:
    print(f"\n--- 第 {rec.get('round')} 轮 · {rec.get('stage')} ---")
    for bot, info in (rec.get("positions") or {}).items():
        if rec.get("round") == 0:
            print(f"  {bot}: {info.get('decision')} — {info.get('reasoning')}")
        else:
            flag = "改口" if info.get("changed") else "坚持"
            print(f"  {bot}: {info.get('was')} → {info.get('now')} [{flag}] {info.get('reasoning')}")
