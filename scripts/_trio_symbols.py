# -*- coding: utf-8 -*-
"""跑 disc-trio：BTC/ETH/SOL 三币分析 + 工具用量 + 讨论过程。"""
import json
import os
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

sys.path.insert(0, str(ROOT))
from omnialpha.config import load_all_bots  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402
from omnialpha.persona.config import load_persona_groups  # noqa: E402
from omnialpha.persona.runner import PersonaRunner  # noqa: E402
import omnialpha.__main__ as m  # noqa: E402

paths = ProjectPaths(ROOT)
bots = load_all_bots(ROOT / "config" / "bots")
groups = load_persona_groups(ROOT / "config" / "persona_groups.yaml")
g = next((x for x in groups if x.name == "disc-trio"), None)

runners = {bid: m._build_plan_runner(bots[bid], paths) for bid in g.members}
r = PersonaRunner(ROOT, g, bots, runners)
res = r.run_once(trigger="manual")

print("=== 最终决策 ===")
print("决策:", res.get("decision"), "动作:", res.get("action"))
print("融合:", json.dumps(res.get("fusion"), ensure_ascii=False))

print("\n=== 各人格分析（按币种）===")
for bid in g.members:
    print(f"\n--- {bid} ---")
    t = getattr(runners[bid], "_tool_usage_summary", lambda: {})()
    print(f"  工具 {t.get('total_calls',0)} 次: {t.get('counts')}  查数据={t.get('data_checked')}")
    # 工具明细
    for u in (getattr(runners[bid], "tool_usage", []) or [])[:20]:
        print(f"    · {u.get('tool')} {u.get('args')}")

print("\n=== 讨论过程 ===")
for rec in res.get("discussion_log") or []:
    print(f"\n第 {rec.get('round')} 轮 · {rec.get('stage')}")
    for bot, info in (rec.get("positions") or {}).items():
        if rec.get("round") == 0:
            print(f"  {bot}: {info.get('decision')} — {info.get('reasoning')}")
        else:
            flag = "改口" if info.get("changed") else "坚持"
            print(f"  {bot}: {info.get('was')}→{info.get('now')} [{flag}] {info.get('reasoning')}")

out = ROOT / "scripts" / "_trio_symbols.json"
out.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(f"\n已保存 {out}")
