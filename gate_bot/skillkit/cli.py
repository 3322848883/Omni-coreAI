"""skill 子命令：install / list / show / validate / remove / run。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from .loader import load_package
from .models import SkillError
from .registry import SkillRegistry
from .validate import validate_package


def default_skills_root(root: Path) -> Path:
    return Path(root) / "skills"


def _registry(root: Path) -> SkillRegistry:
    reg = SkillRegistry()
    roots = [Path(root) / ".mimocode" / "skills", default_skills_root(root)]
    reg.scan(roots)
    return reg


def cmd_skill(args: Any) -> int:
    """入口：按 args.skill_cmd 分派。"""
    root = Path(args.root).resolve() if getattr(args, "root", None) else Path.cwd().resolve()
    sub = getattr(args, "skill_cmd", None)
    if sub == "install":
        return _install(args, root)
    if sub == "list":
        return _list(args, root)
    if sub == "show":
        return _show(args, root)
    if sub == "validate":
        return _validate(args)
    if sub == "remove":
        return _remove(args, root)
    if sub == "run":
        return _run(args, root)
    if sub == "doctor":
        return _doctor(args, root)
    print("usage: gate_bot skill {install,list,show,validate,remove,run,doctor}", file=sys.stderr)
    return 2


# 乱码还原候选编码（UTF-8 字节被误解为这些编码 → 名字二次编码）
_MOJIBAKE_ENCODINGS = ("cp866", "cp437", "cp850")


def demojibake(name: str) -> Optional[str]:
    """尝试还原乱码目录/文件名；无法还原返回 None。"""
    for enc in _MOJIBAKE_ENCODINGS:
        try:
            fixed = name.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != name and not fixed.isascii():
            return fixed
    return None


def _doctor(args: Any, root: Path) -> int:
    """体检已装 skill：乱码名 / 校验 / 结构。--fix 时自动修复乱码名。"""
    fix = bool(getattr(args, "fix", False))
    skills_root = default_skills_root(root)
    if not skills_root.is_dir():
        print(f"(no skills dir: {skills_root})")
        return 0

    issues = 0
    for skill_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
        print(f"\n[{skill_dir.name}]")
        # 1) 乱码名扫描
        bad = []
        for p in skill_dir.rglob("*"):
            if p.name.isascii():
                continue
            fixed = demojibake(p.name)
            if fixed:
                bad.append((p, fixed))
        if bad:
            issues += len(bad)
            for p, fixed in bad:
                rel = p.relative_to(skill_dir)
                print(f"  MOJIBAKE {rel}  ->  {fixed}")
                if fix:
                    target = p.with_name(fixed)
                    if target.exists():
                        print(f"    (目标已存在，跳过)")
                    else:
                        p.rename(target)
                        print(f"    renamed OK")
            if not fix:
                print("  (用 --fix 自动修复)")
        else:
            print("  乱码名: 无")

        # 2) 校验
        rep = validate_package(skill_dir)
        print(f"  validate: {'PASS' if rep.ok else 'FAIL'}  "
              f"errors={len(rep.errors)} warnings={len(rep.warnings)}")
        for e in rep.errors:
            print(f"    ERROR {e}")
        for w in rep.warnings:
            print(f"    WARN  {w}")
        if not rep.ok:
            issues += 1

    print(f"\n体检完成：{issues} 个问题" + ("（已修复乱码名）" if fix else ""))
    return 0 if issues == 0 else 1


def _install(args: Any, root: Path) -> int:
    src = Path(args.path).resolve()
    rep = validate_package(src)
    print(rep.summary())
    if not rep.ok:
        print("install refused (fail-closed)", file=sys.stderr)
        return 1
    pkg = load_package(src)
    dest = default_skills_root(root) / pkg.meta.id
    if dest.exists():
        if not getattr(args, "yes", False):
            print(f"destination exists: {dest} (use --yes to overwrite)", file=sys.stderr)
            return 1
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    (dest / ".installed").write_text("ok\n", encoding="utf-8")
    print(f"installed: {dest}")
    print("NOT auto-enabled. Enable per bot via config/bots/<id>.yaml  skills: [%s]" % pkg.meta.id)
    return 0


def _list(args: Any, root: Path) -> int:
    reg = _registry(root)
    metas = reg.all_metas()
    if not metas:
        print("(no skills installed)")
        return 0
    bot = getattr(args, "bot", None)
    enabled: Optional[list[str]] = None
    if bot:
        enabled = _bot_skills(root, bot)
    print(f"{'ID':<28} {'VER':<8} {'TOK':>6} {'MODEL':<6} {'ENABLED':<8} DESC")
    for m in metas:
        if enabled is None:
            state = "-"
        else:
            state = "yes" if (enabled is not None and (enabled == [] and False or m.id in (enabled or []))) else "no"
            if enabled is not None and len(enabled) == 0:
                state = "no"
        print(f"{m.id:<28} {(m.version or '-'):<8} {m.body_tokens:>6} "
              f"{('yes' if m.model_invocation else 'no'):<6} {state:<8} {m.description[:40]}")
    return 0


def _show(args: Any, root: Path) -> int:
    reg = _registry(root)
    meta = reg.get_meta(args.id)
    if meta is None:
        print(f"unknown skill: {args.id}", file=sys.stderr)
        return 1
    pkg = reg.get_package(args.id)
    print(f"id:            {meta.id}")
    print(f"path:          {meta.path}")
    print(f"version:       {meta.version or '-'}")
    print(f"license:       {meta.license or '-'}")
    print(f"compatibility: {meta.compatibility or '-'}")
    print(f"risk_level:    {meta.risk_level}")
    print(f"model_invoke:  {meta.model_invocation}")
    print(f"allowed_tools: {sorted(meta.allowed_tools) if meta.allowed_tools else '(not narrowed)'}")
    print(f"body_tokens:   {meta.body_tokens}")
    print(f"bundled_files: {len(pkg.files) if pkg else 0}")
    print(f"description:   {meta.description}")
    return 0


def _validate(args: Any) -> int:
    target = Path(args.path).resolve()
    if target.is_file() and target.name == "SKILL.md":
        target = target.parent
    rep = validate_package(target)
    print(rep.summary())
    return 0 if rep.ok else 1


def _remove(args: Any, root: Path) -> int:
    dest = default_skills_root(root) / args.id
    if not dest.exists():
        print(f"not installed: {args.id}", file=sys.stderr)
        return 1
    if not getattr(args, "yes", False):
        print(f"will remove {dest} — rerun with --yes to confirm", file=sys.stderr)
        return 1
    shutil.rmtree(dest)
    print(f"removed: {dest}")
    return 0


def _run(args: Any, root: Path) -> int:
    """用户专属 skill 的 CLI 入口（model-invocation=false 也能跑）。"""
    from .tool import run_skill_tool

    reg = _registry(root)
    try:
        out = run_skill_tool(
            reg,
            {"name": args.id},
            bot_id=getattr(args, "bot", "") or "",
            enabled_ids=None,  # CLI 不做 bot 可见性限制
            root=root,
        )
    except SkillError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


def _bot_skills(root: Path, bot_id: str) -> Optional[list[str]]:
    """读 bot yaml 的 skills 键（轻量解析，避免引入全量 config 依赖）。"""
    import re

    yml = Path(root) / "config" / "bots" / f"{bot_id}.yaml"
    if not yml.is_file():
        return None
    text = yml.read_text(encoding="utf-8")
    m = re.search(r"^skills:\s*\[([^\]]*)\]", text, re.MULTILINE)
    if not m:
        # skills: 换行列表形式
        m2 = re.search(r"^skills:\s*\n((?:\s*-\s*.+\n)+)", text, re.MULTILINE)
        if not m2:
            return None
        items = re.findall(r"-\s*([A-Za-z0-9_-]+)", m2.group(1))
        return items
    inner = m.group(1).strip()
    if not inner:
        return []
    return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]


def register_parser(sub: Any) -> None:
    """挂到 argparse 子命令（供 __main__ 调用）。"""
    p = sub.add_parser("skill", help="manage installable skills (skillkit)")
    p.add_argument("--root", help="project root (default: cwd / GATE_BOT_ROOT)")
    ssub = p.add_subparsers(dest="skill_cmd", required=True)

    pi = ssub.add_parser("install", help="validate + install a skill dir (NOT auto-enabled)")
    pi.add_argument("path", help="source skill directory")
    pi.add_argument("--yes", action="store_true", help="overwrite if exists")
    pi.set_defaults(func=cmd_skill)

    pl = ssub.add_parser("list", help="list installed skills")
    pl.add_argument("--bot", help="show enablement for this bot")
    pl.set_defaults(func=cmd_skill)

    ps = ssub.add_parser("show", help="show one skill's metadata")
    ps.add_argument("id")
    ps.set_defaults(func=cmd_skill)

    pv = ssub.add_parser("validate", help="validate a skill dir only")
    pv.add_argument("path")
    pv.set_defaults(func=cmd_skill)

    pr = ssub.add_parser("remove", help="uninstall a skill")
    pr.add_argument("id")
    pr.add_argument("--yes", action="store_true", help="confirm removal")
    pr.set_defaults(func=cmd_skill)

    pn = ssub.add_parser("run", help="load skill body (user-invocable, incl. model-invocation=false)")
    pn.add_argument("id")
    pn.add_argument("--bot", default="")
    pn.set_defaults(func=cmd_skill)

    pd = ssub.add_parser("doctor", help="体检已装 skill（乱码名/校验/结构）")
    pd.add_argument("--fix", action="store_true", help="自动修复乱码目录名")
    pd.set_defaults(func=cmd_skill)
