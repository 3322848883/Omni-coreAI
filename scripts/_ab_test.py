"""对比测试：改造前 vs 改造后，**冻结同一份行情**，只变提示词契约与技能版本。

为什么要冻结行情：拿「08:46 那一轮」和「12:50 那一轮」比是不成立的 —— 行情不同。
真正可控的对照必须让输入一致，只让被测变量不同。

配置：
- A（改造前）：`_SYSTEM_HEAD` 取 base commit 0948b04 的版本（无 region/rule_ids/规则18）
              + 技能取 0948b04 的 skills-src/price-action-trading（3.75MB 原版）
- B（改造后）：当前 `_SYSTEM_HEAD` + 当前 skills-src/price-action-trading

两组各跑 2 次，同一冻结快照。记录：字段是否落进 plan、原始输出里是否出现
region=range+tp2 这种矛盾、CoT 长度、耗时、工具调用。
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE_COMMIT = "0948b04"
SKILL = "price-action-trading"
BOT = "brooks-btc"
SNAPSHOT = ROOT / "scripts" / "_ab_snapshot.json"
REPS = 2

from omnialpha import strategist  # noqa: E402
from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.strategist import loop as loop_mod  # noqa: E402
from omnialpha.strategist import prompt as prompt_mod  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402


def extract_skill_at(commit: str, rel: str, dest_root: Path) -> Path:
    """把某 commit 下的技能目录取到 dest_root，返回技能目录路径。

    用 `git archive` + tarfile，不用 `git ls-tree`/`git show` 逐个取 ——
    后者对非 ASCII 路径会做转义（core.quotepath），实测直接炸在
    `references/knowledge/source/V1_趋势篇` 上。

    **注意 tar 里的路径已含 `rel`**，所以 dest_root 不能再拼一次 rel
    （上一版就多套了一层：`_ab_old/skills-src/<skill>/skills-src/<skill>/…`）。
    """
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    data = subprocess.run(["git", "archive", "--format=tar", commit, rel],
                          capture_output=True, cwd=ROOT).stdout
    if not data:
        raise SystemExit(f"git archive 无输出: {commit}:{rel}")
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(dest_root, filter="data")
    skill_dir = dest_root / rel
    if not (skill_dir / "SKILL.md").is_file():
        raise SystemExit(f"提取后找不到 SKILL.md: {skill_dir}")
    return skill_dir


def install_skill(src_dir: Path) -> int:
    """把技能装到 skills/，并**断言装完与源一致**（防并发写入造成的错装）。

    同时写 `.installed` 标记 —— 与 `omnialpha skill install` 一致（cli.py）。
    少了它会让 `skills/` 看上去「不是 CLI 装的」，也失去可查的安装痕迹。
    """
    dst = ROOT / "skills" / SKILL
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src_dir, dst)
    (dst / ".installed").write_text("ok\n", encoding="utf-8")
    src_body = (src_dir / "SKILL.md").read_bytes()
    dst_body = (dst / "SKILL.md").read_bytes()
    if src_body != dst_body:
        raise SystemExit("安装后 SKILL.md 与源不一致（疑似并发写入）")
    return len(src_body)


def load_old_head() -> str:
    out = subprocess.run(["git", "show", f"{BASE_COMMIT}:omnialpha/strategist/prompt.py"],
                         capture_output=True, cwd=ROOT).stdout.decode("utf-8")
    import ast
    for node in ast.parse(out).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_SYSTEM_HEAD":
            return "".join(n.value for n in ast.walk(node.value)
                           if isinstance(n, ast.Constant) and isinstance(n.value, str))
    raise SystemExit("未找到旧 _SYSTEM_HEAD")


def freeze_snapshot() -> dict:
    """跑一次真实采集并落盘 —— 之后所有对照都复用这一份。"""
    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / f"{BOT}.yaml")
    from omnialpha.__main__ import _build_plan_runner
    runner = _build_plan_runner(bot, paths)
    snap = loop_mod.collect_snapshot(
        runner.client, runner.cfg.symbols, candles=runner.cfg.candles,
        interval=runner.cfg.timeframe, market_cfg=runner.cfg.market,
        env=runner.cfg.env, bot_root=runner.cfg.bot_root,
    )
    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, default=str), encoding="utf-8")
    print("冻结快照 -> %s (%d KB)" % (SNAPSHOT.name, SNAPSHOT.stat().st_size // 1024))
    return snap


def run_one(frozen: dict, tag: str) -> dict:
    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / f"{BOT}.yaml")
    # **隔离被测变量**：brooks-btc 未声明 `strategist.skills` → 对**两个**技能都可见，
    # 而 pa-analysis 的新 SKILL.md 也把 7 个字段列为必填 → 另一个技能的新契约会从
    # 「旧配置」臂泄漏进来。独立评审实测：未隔离时 A 组有一次先调了 skill(pa-analysis)，
    # 于是 A 组 1/2 次出现 region/rule_ids，因果归因失效。这里把白名单收到只留被测技能。
    if isinstance(getattr(bot, "strategist", None), dict):
        bot.strategist["skills"] = [SKILL]
    from omnialpha.__main__ import _build_plan_runner
    runner = _build_plan_runner(bot, paths)

    cap: dict = {}
    orig = runner._chat_with_tools

    def patched(system, user, chart_base64=None):
        cap["system"] = system
        text = orig(system, user, chart_base64=chart_base64)
        cap["text"] = text
        return text

    runner._chat_with_tools = patched
    t0 = time.time()
    res = runner.analyze_once(trigger=f"ab-{tag}")
    dt = time.time() - t0

    plan = (res or {}).get("plan") or {}
    chip = (plan.get("chips") or [{}])[0]
    raw = cap.get("text") or ""
    rc = list(getattr(runner.llm, "last_reasoning_chain", []) or [])
    sys_txt = cap.get("system") or ""
    return {
        "tag": tag,
        "ok": bool((res or {}).get("ok")),
        # 隔离校验：另一个技能不得出现在 L1 catalog 里（否则对照组被污染）
        "catalog_isolated": ("pa-analysis" not in sys_txt),
        "called_other_skill": any(
            u["tool"] == "skill" and (u.get("args") or {}).get("name") == "pa-analysis"
            for u in runner.tool_usage),
        "secs": round(dt, 1),
        "sys_len": len(cap.get("system") or ""),
        "cot_chars": sum(len(s) for s in rc),
        "cot_segs": len(rc),
        "tools": [u["tool"] for u in runner.tool_usage],
        "chip": chip,
        "raw_has_region": '"region"' in raw,
        "raw_has_rule_ids": '"rule_ids"' in raw,
        "raw_has_tp2_value": ('"tp2"' in raw and '"tp2":null' not in raw.replace(" ", "")),
        "raw_has_range": '"range"' in raw,
        "decision": plan.get("decision"),
    }


FIELDS = ["region", "invalidation", "time_stop_bars", "give_back_pct", "risk_pct",
          "rule_ids", "scenarios", "tp2"]


def main() -> int:
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.is_file() else freeze_snapshot()
    # 所有对照都用这份冻结快照
    loop_mod.collect_snapshot = lambda *a, **kw: frozen

    new_head = prompt_mod._SYSTEM_HEAD
    old_head = load_old_head()
    # **必须 patch `SYSTEM_PROMPT` 而不是 `_SYSTEM_HEAD`**：前者在 import 时就拼好了
    # （prompt.py:116 `SYSTEM_PROMPT = _SYSTEM_HEAD + PLAN_SCHEMA_HINT`），
    # `build_system_prompt` 读的是它。只 patch `_SYSTEM_HEAD` 等于什么都没改 ——
    # 第一版 A/B 就栽在这里（两组其实用了同一份契约，结果无意义）。
    new_sys = prompt_mod.SYSTEM_PROMPT

    # 取出 base commit 的原版技能
    old_skill_src = extract_skill_at(BASE_COMMIT, f"skills-src/{SKILL}",
                                     ROOT / "scripts" / "_ab_old")
    print("base commit 原版技能: %d 个文件 (%s)"
          % (len([p for p in old_skill_src.rglob("*") if p.is_file()]),
             old_skill_src.relative_to(ROOT)))

    results: list[dict] = []
    plan_cfg = (("A-改造前", old_head, old_skill_src),
                ("B-改造后", new_head, ROOT / "skills-src" / SKILL))
    # **try/finally 复原**：本脚本会重写 `skills/`（gitignore，无版本兜底）。
    # 中途异常若不复原，会把安装目录留在改造前那份旧技能上，且没有任何标记可查
    # （独立评审指出）。
    try:
        for cfg, head, skill_src in plan_cfg:
            body_len = install_skill(skill_src)
            prompt_mod.SYSTEM_PROMPT = head + prompt_mod.PLAN_SCHEMA_HINT
            exp = len(prompt_mod.SYSTEM_PROMPT)
            print("  [%s] 契约 %d 字符 + SKILL.md %d 字节 -> SYSTEM_PROMPT %d"
                  % (cfg, len(head), body_len, exp))
            for rep in range(1, REPS + 1):
                r = run_one(frozen, f"{cfg}-{rep}")
                results.append(r)
                print("  %s rep%d: %s 秒  CoT %d 字符  工具 %d 个  decision=%s"
                      % (cfg, rep, r["secs"], r["cot_chars"], len(r["tools"]), r["decision"]))
    finally:
        prompt_mod.SYSTEM_PROMPT = new_sys
        install_skill(ROOT / "skills-src" / SKILL)   # 复原

    # 配置差异断言：A 的 system 必须显著短于 B（旧契约 -511、旧技能正文短约 2,146）
    a_sys = [r["sys_len"] for r in results if r["tag"].startswith("A")]
    b_sys = [r["sys_len"] for r in results if r["tag"].startswith("B")]
    print("\n配置差异校验: A 组 system=%s  B 组 system=%s  差=%d（应约 +2,657）"
          % (a_sys, b_sys, (b_sys[0] - a_sys[0]) if a_sys and b_sys else 0))

    print("\n" + "=" * 78)
    print("字段是否落进 plan（A=改造前，B=改造后）")
    print("=" * 78)
    hdr = "%-16s" % "字段" + "".join("%-14s" % f"{r['tag']}" for r in results)
    print(hdr)
    for f in FIELDS:
        row = "%-16s" % f
        for r in results:
            v = r["chip"].get(f)
            if f == "scenarios":
                cell = "有(%d)" % len(v) if isinstance(v, dict) and v else "无"
            elif f == "rule_ids":
                cell = "有(%d)" % len(v) if isinstance(v, list) and v else "无"
            elif v is None:
                cell = "-"
            else:
                cell = "有"
            row += "%-14s" % cell
        print(row)

    print("\n" + "=" * 78)
    print("原始输出层面的信号")
    print("=" * 78)
    print("%-16s" % "指标" + "".join("%-14s" % r["tag"] for r in results))
    for key, label in (("catalog_isolated", "catalog 已隔离"), ("called_other_skill", "调了另一技能"),
                       ("raw_has_region", "原始含 region"), ("raw_has_rule_ids", "原始含 rule_ids"),
                       ("raw_has_range", "原始含 range"), ("raw_has_tp2_value", "原始含 tp2 非null"),
                       ("sys_len", "system 长度"), ("cot_chars", "CoT 字符"),
                       ("secs", "耗时秒")):
        print("%-16s" % label + "".join("%-14s" % r[key] for r in results))

    out = ROOT / "scripts" / "_ab_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n明细 -> %s" % out.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
