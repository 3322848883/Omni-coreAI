"""A/B 对比：同人格同图，唯一差异 = 是否加载 skill。"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config" / "bots" / "skill-ab.yaml"
JOURNAL = ROOT / "logs" / "skill_journal.jsonl"
STATE = ROOT / "data" / "bots" / "skill-ab" / "state"


def set_skills(ids):
    text = CFG.read_text(encoding="utf-8")
    line = "  skills: []" if not ids else f"  skills: [{', '.join(ids)}]"
    if re.search(r"^  skills:", text, re.MULTILINE):
        text = re.sub(r"^  skills:.*$", line, text, flags=re.MULTILINE)
    else:
        text = text.replace("  vision_timeframes:", line + "\n  vision_timeframes:")
    CFG.write_text(text, encoding="utf-8")


def journal_count():
    return sum(1 for _ in JOURNAL.open(encoding="utf-8")) if JOURNAL.is_file() else 0


def run(tag):
    j0 = journal_count()
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, "-m", "gate_bot", "plan", "--bot", "skill-ab"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), timeout=300,
    )
    dt = time.time() - t0
    j1 = journal_count()
    docs = sorted(STATE.glob("*.hold.json"), key=lambda p: p.stat().st_mtime)
    th = sorted(STATE.glob("*.thinking.json"), key=lambda p: p.stat().st_mtime)
    hold = json.loads(docs[-1].read_text(encoding="utf-8")) if docs else {}
    think = json.loads(th[-1].read_text(encoding="utf-8")) if th else {}
    reasoning = hold.get("reasoning") or ""
    chip = (hold.get("chips") or [{}])[0]
    return {
        "tag": tag, "rc": r.returncode, "sec": round(dt, 1),
        "skill_activated": j1 > j0,
        "reasoning_len": len(reasoning),
        "reasoning": reasoning,
        "chip_action": chip.get("action"),
        "chip_conf": chip.get("confidence"),
        "thinking_rounds": len(think.get("reasoning_chain") or []),
        "stderr": (r.stdout + r.stderr)[:200],
    }


def main():
    print("=" * 60)
    print("A/B 对比测试：价格行为人格 + 4周期图，唯一差异=skill")
    print("=" * 60)

    print("\n[组 A] 不使用 skill（skills: []）")
    set_skills([])
    a = run("A-no-skill")
    print(f"  rc={a['rc']} {a['sec']}s  skill_activated={a['skill_activated']}")
    print(f"  reasoning 长度={a['reasoning_len']}  chip={a['chip_action']} conf={a['chip_conf']}")
    print(f"  thinking 轮数={a['thinking_rounds']}")

    print("\n[组 B] 使用 skill（skills: [price-action-trading]）")
    set_skills(["price-action-trading"])
    b = run("B-with-skill")
    print(f"  rc={b['rc']} {b['sec']}s  skill_activated={b['skill_activated']}")
    print(f"  reasoning 长度={b['reasoning_len']}  chip={b['chip_action']} conf={b['chip_conf']}")
    print(f"  thinking 轮数={b['thinking_rounds']}")

    # 保存
    out = ROOT / "verify_data" / "skill-ab-comparison.md"
    def sect(label, d):
        return (f"## {label}\n\n"
                f"- skill 激活: {d['skill_activated']}\n"
                f"- Plan: {d['chip_action']} (confidence {d['chip_conf']})\n"
                f"- reasoning 长度: {d['reasoning_len']} 字符\n"
                f"- thinking 轮数: {d['thinking_rounds']}\n"
                f"- 耗时: {d['sec']}s\n\n"
                f"### reasoning 全文\n\n{d['reasoning']}\n")
    out.write_text(
        "# A/B 对比：价格行为人格 + 4周期图（skill vs 无 skill）\n\n"
        "> 同 bot 配置 / 同人格 `prompts/brooks_btc_pa.md` / 同 4 周期图 / 同 BTC_USDT\n"
        "> 唯一变量：`skills: []` vs `skills: [price-action-trading]`\n\n"
        + sect("组 A — 不使用 skill", a) + "\n---\n\n" + sect("组 B — 使用 skill", b),
        encoding="utf-8",
    )
    print(f"\n保存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
