#!/usr/bin/env python3
"""打包 skill 为 zip（UTF-8 文件名安全）。

用法：python scripts/pack_skill.py <skill-id|path> [-o out.zip]

为什么不用系统 zip/unzip：Windows 的 zip 与 Linux 的 unzip 对非 ASCII
文件名处理不一致（UTF-8 标志位缺失 → CP866 误解 → 乱码）。本脚本统一用
Python zipfile（强制 UTF-8 标志位），解压侧也用 Python 即可无损往返。
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "skills-src"

# 不打包的运行时产物
EXCLUDE_DIRS = {".git", "__pycache__", "logs", ".pytest_cache"}
EXCLUDE_FILES = {".installed", ".DS_Store"}


def resolve(target: str) -> Path:
    p = Path(target)
    if p.is_dir():
        return p
    cand = SRC_ROOT / target
    if cand.is_dir():
        return cand
    raise SystemExit(f"skill not found: {target}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="skill id (under skills-src/) or path")
    ap.add_argument("-o", "--out", help="output zip path")
    args = ap.parse_args()

    src = resolve(args.target).resolve()
    if not (src / "SKILL.md").is_file():
        raise SystemExit(f"not a skill dir (no SKILL.md): {src}")

    out = Path(args.out) if args.out else (ROOT / f"{src.name}.zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if p.name in EXCLUDE_FILES:
                continue
            if p.is_file():
                # zipfile 默认对非 ASCII 名写 UTF-8 标志位
                z.write(p, arcname=f"{src.name}/{rel.as_posix()}")
                n += 1
    print(f"packed {n} files -> {out}")
    print(f"size: {out.stat().st_size:,} bytes")

    # 自检：验证往返无损（用 Python 解压侧读回）
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    non_ascii = [x for x in names if not x.isascii()]
    print(f"非 ASCII 条目: {len(non_ascii)}（示例: {non_ascii[:2]}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
