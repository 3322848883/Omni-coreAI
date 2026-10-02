"""SkillKit LLM 多场景生产测试：4 类任务 × 真实 LLM。

场景：
  A 自然触发（分析 ETH，不点名 skill）
  B 规则检查（交易计划合规）
  C 知识查询（H2 / Always In）
  D 复盘（错误模式分析）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_YAML = ROOT / "config" / "bots" / "skill-e2e.yaml"
JOURNAL = ROOT / "logs" / "skill_journal.jsonl"

SCENARIOS = {
    "A-natural": "prompts/skill_e2e_natural.md",
    "B-checklist": "prompts/skill_e2e_checklist.md",
    "C-knowledge": "prompts/skill_e2e_knowledge.md",
    "D-review": "prompts/skill_e2e_review.md",
}

# 各场景应命中的方法论关键词（来自 skill body/knowledge）
EXPECT = {
    "A-natural": ["Always In", "trading range", "BAN"],
    "B-checklist": ["BAN", "SL", "止损"],
    "C-knowledge": ["H2", "Always In"],
    "D-review": ["失败", "错误", "改进"],
}


def journal_count() -> int:
    if not JOURNAL.is_file():
        return 0
    return sum(1 for _ in JOURNAL.open(encoding="utf-8"))


def set_prompt(prompt_file: str) -> None:
    text = BOT_YAML.read_text(encoding="utf-8")
    text = re.sub(r"prompt_file: .*", f"prompt_file: {prompt_file}", text)
    BOT_YAML.write_text(text, encoding="utf-8")


def run_plan() -> tuple[int, str]:
    env = {"PYTHONPATH": str(ROOT)}
    r = subprocess.run(
        [sys.executable, "-m", "omnialpha", "plan", "--bot", "skill-e2e"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), timeout=180,
    )
    return r.returncode, r.stdout + r.stderr


def latest_thinking() -> dict:
    state = ROOT / "data" / "bots" / "skill-e2e" / "state"
    files = sorted(state.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return {}
    return json.loads(files[-1].read_text(encoding="utf-8"))


def main() -> int:
    print("=" * 60)
    print("SkillKit LLM 多场景生产测试")
    print("=" * 60)
    results = []
    for name, prompt in SCENARIOS.items():
        print(f"\n── 场景 {name}（{prompt}）──")
        j0 = journal_count()
        set_prompt(prompt)
        t0 = time.time()
        code, out = run_plan()
        dt = time.time() - t0
        j1 = journal_count()
        activated = j1 > j0

        think = latest_thinking()
        chain = " ".join(think.get("reasoning_chain") or [])
        hits = [w for w in EXPECT[name] if w in chain]
        ok_plan = '"ok"' in out or '"ok": true' in out

        print(f"  plan rc={code}  {dt:.1f}s  ok={ok_plan}")
        print(f"  skill_activate 新增: {activated} (journal {j0}->{j1})")
        print(f"  方法论关键词命中: {hits}")
        print(f"  thinking 长度: {len(chain)}")
        results.append({
            "scene": name, "rc": code, "ok_plan": ok_plan,
            "skill_activated": activated, "keywords": hits,
            "think_len": len(chain),
        })

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    all_ok = True
    for r in results:
        # skill 激活是硬指标；关键词 ≥1 命中是软指标
        gate = r["skill_activated"] and r["rc"] == 0
        kw_ok = len(r["keywords"]) >= 1
        status = "PASS" if (gate and kw_ok) else ("SOFT" if gate else "FAIL")
        if status == "FAIL":
            all_ok = False
        print(f"  [{status}] {r['scene']:<12} skill={r['skill_activated']} kw={r['keywords']} think={r['think_len']}")

    print(f"\n结果: {'ALL PASS' if all_ok else 'HAS FAILURES'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
