"""SkillKit 生产测试：用 price-action-trading-v34.2 真实包走完整生命周期。

只测本地，不碰服务器。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.skillkit import (  # noqa: E402
    SkillRegistry,
    render_catalog,
    run_skill_tool,
    validate_package,
)
from omnialpha.strategist.tools import NATIVE_TOOLS, TOOL_NAMES, run_tool  # noqa: E402

ZIP = Path(r"C:\Users\w6485\Desktop\price-action-trading-v34.2.zip")
WORK = ROOT / "tmp_prod_test"
SKILLS = ROOT / "skills"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 60)
    print("SkillKit 生产测试 — price-action-trading-v34.2")
    print("=" * 60)

    # 0) 解压真实包
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir()
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(WORK)
    src = WORK / "price-action-trading"
    check("01 zip 解压", src.is_dir(), str(src))

    # 1) validate 真实包
    rep = validate_package(src)
    check("02 validate 真实包 PASS", rep.ok, rep.summary().splitlines()[0])

    # 2) CLI validate / install
    py = sys.executable
    installed = SKILLS / "price-action-trading"

    def cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [py, "-m", "omnialpha", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT),
        )

    r = cli("skill", "validate", str(src))
    check("03 CLI validate 退出码 0", r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[0])

    # 干净重装
    if installed.exists():
        shutil.rmtree(installed)
    r = cli("skill", "install", str(src))
    check("04 CLI install 成功", r.returncode == 0 and installed.is_dir(), f"→ {installed}")
    check("05 install 不自动启用", "NOT auto-enabled" in (r.stdout + r.stderr))

    # 3) list / show
    r = cli("skill", "list")
    check("06 CLI list 显示已装", "price-action-trading" in r.stdout)
    r = cli("skill", "show", "price-action-trading")
    check("07 CLI show 元数据完整", "version:       34.2" in r.stdout and "body_tokens:" in r.stdout)

    # 4) registry + catalog（L1）
    reg = SkillRegistry()
    metas = reg.scan([ROOT / ".mimocode" / "skills", SKILLS])
    check("08 registry 扫描到", any(m.id == "price-action-trading" for m in metas), f"{[m.id for m in metas]}")

    cat_all = render_catalog(reg.visible_for("anybot"))
    check("09 catalog 渲染（默认全可见）", "<skill_catalog>" in cat_all and "price-action-trading" in cat_all)
    check("10 catalog 只含 name+desc（无 body）", "# Al Brooks" not in cat_all and "26 步" in cat_all)

    cat_none = render_catalog(reg.visible_for("anybot", []))
    check("11 skills:[] 不渲染 catalog", cat_none == "")

    cat_one = render_catalog(reg.visible_for("anybot", ["price-action-trading"]))
    check("12 白名单可见集", "price-action-trading" in cat_one)

    # 5) skill 工具（L2）
    out = run_skill_tool(
        reg, {"name": "price-action-trading"},
        bot_id="prod-test", enabled_ids=["price-action-trading"], root=ROOT,
    )
    check("13 skill 工具载入 body", out.startswith("[skill:price-action-trading]"))
    check("14 body 含核心指令", "26 步" in out and "references/SOUL.md" in out)
    check("15 body 带结束标记", out.rstrip().endswith("[/skill:price-action-trading]"))

    journal = ROOT / "logs" / "skill_journal.jsonl"
    check("16 审计 journal 已写", journal.is_file() and "skill_activate" in journal.read_text(encoding="utf-8"))

    # 6) run_tool 集成（第 21 个工具）
    check("17 NATIVE_TOOLS=21 且含 skill", len(NATIVE_TOOLS) == 21 and "skill" in TOOL_NAMES)
    r2 = run_tool(None, "skill", {"name": "price-action-trading"}, bot_root=str(ROOT))
    check("18 run_tool 分发成功", isinstance(r2, dict) and r2.get("skill") == "price-action-trading")

    # 7) 负面：未启用 / 未知 / 坏包
    try:
        run_skill_tool(reg, {"name": "price-action-trading"}, bot_id="x", enabled_ids=["other"])
        check("19 未启用被拒", False)
    except Exception as e:
        check("19 未启用被拒", "not enabled" in str(e))

    try:
        run_skill_tool(reg, {"name": "nope"})
        check("20 未知 skill 被拒", False)
    except Exception as e:
        check("20 未知 skill 被拒", "unknown skill" in str(e))

    # 坏包：尖括号注入
    bad = WORK / "evil-skill"
    bad.mkdir(exist_ok=True)
    (bad / "SKILL.md").write_text(
        '---\nname: evil-skill\ndescription: "Use when <b>evil</b>."\n---\n\nbad',
        encoding="utf-8",
    )
    rep_bad = validate_package(bad)
    check("21 坏包（尖括号）拒装", (not rep_bad.ok) and any("E05" in e for e in rep_bad.errors))
    r = cli("skill", "validate", str(bad))
    check("22 CLI 对坏包退出码 1", r.returncode == 1)

    # 坏包：README
    bad2 = WORK / "readme-skill"
    bad2.mkdir(exist_ok=True)
    (bad2 / "SKILL.md").write_text(
        f'---\nname: readme-skill\ndescription: "Use when testing readme skill case."\n---\n\nok',
        encoding="utf-8",
    )
    (bad2 / "README.md").write_text("x", encoding="utf-8")
    rep2 = validate_package(bad2)
    check("23 坏包（README）拒装", any("E03" in e for e in rep2.errors))

    # 8) CLI run（用户入口）
    r = cli("skill", "run", "price-action-trading")
    check("24 CLI run 输出 body", r.returncode == 0 and "[skill:price-action-trading]" in r.stdout)

    # 9) 卸载需确认
    r = cli("skill", "remove", "price-action-trading")
    check("25 remove 无 --yes 被拒", r.returncode == 1 and installed.is_dir())

    # ── 汇总 ──
    print("=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} {detail}")
    print(f"结果：{passed}/{total} PASS")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
