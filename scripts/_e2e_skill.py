"""T14 端到端验证：brooks-btc 跑一轮 analyze_once，看技能是否真被读、chip 是否填了新字段。

验收（方案 T14）：
- `logs/skill_journal.jsonl` 新增 `skill_activate` ≥1 且 `skill_ref_read` ≥1，且**无截断**
- 返回的 chip 含 `region` 与 `rule_ids`

用 analyze_once（只读路径）：LLM → Plan，**不写 inbox、不下单**。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.__main__ import _build_plan_runner  # noqa: E402
from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402

JOURNAL = ROOT / "logs" / "skill_journal.jsonl"
BOT = "brooks-btc"


def _journal_lines() -> list[dict]:
    if not JOURNAL.is_file():
        return []
    out = []
    for ln in JOURNAL.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def main() -> int:
    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / f"{BOT}.yaml")
    print("bot=%s env=%s enabled=%s" % (bot.bot_id, bot.env, getattr(bot, "enabled", None)))
    st = getattr(bot, "strategist", None) or {}
    print("strategist.skills(白名单)=%s" % (st.get("skills") if isinstance(st, dict) else "?"))

    before = len(_journal_lines())
    runner = _build_plan_runner(bot, paths)

    cap: dict = {}
    orig = runner._chat_with_tools

    def patched(system, user, chart_base64=None):
        cap["system"] = system
        cap["user"] = user
        return orig(system, user, chart_base64=chart_base64)

    runner._chat_with_tools = patched

    t0 = time.time()
    res = runner.analyze_once(trigger="e2e-skill-verify")
    dt = time.time() - t0

    print("\n=== 耗时 %.1fs ===" % dt)
    print("技能目录是否进入 L1 catalog:", "<skill_catalog>" in cap.get("system", ""))
    for name in ("price-action-trading", "pa-analysis"):
        print("  catalog 含 %-22s %s" % (name, name in cap.get("system", "")))

    counts: dict[str, int] = {}
    for u in runner.tool_usage:
        counts[u["tool"]] = counts.get(u["tool"], 0) + 1
    print("\n=== 工具调用 ===")
    print("  %s" % counts)

    after = _journal_lines()[before:]
    acts = [r for r in after if r.get("kind") == "skill_activate"]
    refs = [r for r in after if r.get("kind") == "skill_ref_read"]
    print("\n=== skill journal 新增 ===")
    print("  skill_activate  %d 次: %s" % (len(acts), [r.get("skill_id") for r in acts]))
    print("  skill_ref_read  %d 次:" % len(refs))
    for r in refs:
        print("     %-58s truncated=%s" % (r.get("path"), r.get("truncated")))

    print("\n=== 返回的 chip ===")
    plan = (res or {}).get("plan") or {}
    chips = plan.get("chips") or []
    if not chips:
        print("  (无 chip)")
    for c in chips:
        for k in ("action", "symbol", "type", "size_usd", "price", "sl", "tp", "tp2",
                  "region", "invalidation", "time_stop_bars", "give_back_pct",
                  "risk_pct", "rule_ids", "scenarios"):
            if k in c:
                print("  %-16s %s" % (k, c[k]))
    print("  decision=%s confidence=%s" % (plan.get("decision"), plan.get("confidence")))
    print("  reasoning=%s" % str(plan.get("reasoning"))[:80])

    ok_act = len(acts) >= 1
    ok_ref = len(refs) >= 1 and not any(r.get("truncated") for r in refs)
    ok_chip = bool(chips) and chips[0].get("region") and chips[0].get("rule_ids")
    print("\n=== T14 验收 ===")
    print("  skill_activate >=1      : %s" % ok_act)
    print("  skill_ref_read >=1 无截断: %s" % ok_ref)
    print("  chip 含 region/rule_ids : %s" % ok_chip)
    return 0 if (ok_act and ok_ref and ok_chip) else 1


if __name__ == "__main__":
    raise SystemExit(main())
