# -*- coding: utf-8 -*-
"""读取 3 轮结果，输出综合分析。"""
import json
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\OmniAlpha")
runs = json.loads((ROOT / "scripts" / "_trio_final_3runs.json").read_text(encoding="utf-8"))

for rec in runs:
    print("=" * 72)
    print(f"第 {rec['run']} 轮  决策={rec['decision']} 动作={rec['action']} 置信={rec['confidence']} 讨论轮={rec['discussion_rounds']}")
    print("=" * 72)
    print("工具用量:")
    for bid, t in rec["tools"].items():
        s = t["summary"]
        print(f"  {bid}: {s.get('total_calls',0)}次 查数据={s.get('data_checked')}")
        for d in t["detail"]:
            print(f"      {d['tool']} {d['args']}")
    print("讨论:")
    for rr in rec.get("discussion_log") or []:
        print(f"  [第{rr['round']}轮 {rr['stage']}]")
        for bot, info in (rr.get("positions") or {}).items():
            if rr["round"] == 0:
                print(f"    {bot}: {info.get('decision')} — {str(info.get('reasoning'))[:70]}")
            else:
                fl = "★改口" if info.get("changed") else "坚持"
                print(f"    {bot}: {info.get('was')}→{info.get('now')} [{fl}] {str(info.get('reasoning'))[:70]}")
    print()
