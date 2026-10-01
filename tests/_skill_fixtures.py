"""SkillKit 测试夹具助手。

把 tests/fixtures/skills/ 下的测试 skill 复制进一个临时 root 的 skills/，
使 run_tool 的扫描（<root>/skills）能发现它们，同时生产 skills/ 保持干净。
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "skills"
REAL_SKILLS = ROOT / "skills"

_TMP: Path | None = None


def fixture_root() -> Path:
    """返回一个含 skills/ 的临时 root（fixtures + 真实 skill 合并在内）。"""
    global _TMP
    if _TMP is not None:
        return _TMP
    _TMP = Path(tempfile.mkdtemp(prefix="skillfix-"))
    dst = _TMP / "skills"
    dst.mkdir(parents=True, exist_ok=True)
    for src_dir in (FIXTURES, REAL_SKILLS):
        if not src_dir.is_dir():
            continue
        for p in src_dir.iterdir():
            if p.is_dir():
                target = dst / p.name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(p, target)
    atexit.register(_cleanup)
    return _TMP


def _cleanup() -> None:
    global _TMP
    if _TMP is not None and _TMP.exists():
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP = None


def fixture_skills_dir() -> Path:
    """临时 root 下的 skills/ 目录。"""
    return fixture_root() / "skills"
