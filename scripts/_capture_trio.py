# -*- coding: utf-8 -*-
"""抓取三人格各自完整分析（含 LLM 思考链 + 工具调用 + 决策）。"""
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
import gate_bot.__main__ as m  # noqa: E402

paths = ProjectPaths(ROOT)
bots = load_all_bots(ROOT / "config" / "bots")

results = {}
for bot_id in ["pa-a", "smc-paper", "orderflow-paper"]:
    bot = bots.get(bot_id)
    if bot is None:
        print(f"!! {bot_id} 无配置")
        continue
    print(f"\n{'='*70}\n### {bot_id}  分析中...\n{'='*70}")
    runner = m._build_plan_runner(bot, paths)
    res = runner.analyze_once(trigger="manual")
    # 抓 LLM 思考链
    chain = list(getattr(runner.llm, "last_reasoning_chain", []) or [])
    results[bot_id] = {
        "ok": res.get("ok"),
        "plan": res.get("plan"),
        "error": res.get("error"),
        "reasoning_chain": chain,
        "tool_usage": list(getattr(runner, "tool_usage", []) or []),
        "tool_summary": getattr(runner, "_tool_usage_summary", lambda: {})(),
    }
    print(f"  ok={res.get('ok')}  思考段={len(chain)}  工具={len(results[bot_id]['tool_usage'])}")

out = ROOT / "scripts" / "_trio_full_capture.json"
out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(f"\n已保存: {out}")
