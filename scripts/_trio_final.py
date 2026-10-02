# -*- coding: utf-8 -*-
"""综合测试：3 轮 disc-trio，完整记录工具用量/决策/讨论改口。"""
import json
import os
import sys
import time
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

sys.path.insert(0, str(ROOT))
from omnialpha.config import load_all_bots  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402
from omnialpha.persona.config import load_persona_groups  # noqa: E402
from omnialpha.persona.runner import PersonaRunner  # noqa: E402
import omnialpha.__main__ as m  # noqa: E402

ROUNDS = 3
paths = ProjectPaths(ROOT)
bots = load_all_bots(ROOT / "config" / "bots")
groups = load_persona_groups(ROOT / "config" / "persona_groups.yaml")
g = next((x for x in groups if x.name == "disc-trio"), None)

runs = []
for n in range(1, ROUNDS + 1):
    print(f"\n{'='*72}\n第 {n} 轮\n{'='*72}")
    runners = {bid: m._build_plan_runner(bots[bid], paths) for bid in g.members}
    r = PersonaRunner(ROOT, g, bots, runners)
    res = r.run_once(trigger="manual")
    tools = {}
    for bid in g.members:
        t = getattr(runners[bid], "_tool_usage_summary", lambda: {})()
        detail = [{"tool": u.get("tool"), "args": u.get("args")}
                  for u in (getattr(runners[bid], "tool_usage", []) or [])]
        tools[bid] = {"summary": t, "detail": detail}
    rec = {
        "run": n, "decision": res.get("decision"), "action": res.get("action"),
        "confidence": (res.get("fusion") or {}).get("confidence"),
        "votes": (res.get("fusion") or {}).get("votes"),
        "discussion_rounds": (res.get("fusion") or {}).get("discussion_rounds"),
        "tools": tools, "discussion_log": res.get("discussion_log"), "ok": res.get("ok"),
    }
    runs.append(rec)
    print(f"决策={rec['decision']} 动作={rec['action']} 置信={rec['confidence']}")
    for bid in g.members:
        s = tools[bid]["summary"]
        print(f"  {bid}: {s.get('total_calls',0)}次 查数据={s.get('data_checked')} {s.get('counts')}")

out = ROOT / "scripts" / "_trio_final_3runs.json"
out.write_text(json.dumps(runs, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(f"\n保存: {out}")
