#!/usr/bin/env python3
"""修复 unzip 导致的中文目录名乱码。

原理：UTF-8 字节被 unzip 按 CP866 解码 → 名字再以 UTF-8 存盘。
还原：name.encode(CP866) 得到原始 UTF-8 字节 → decode('utf-8')。
"""
from pathlib import Path

ROOT = Path("/opt/omnialpha/skills/price-action-trading/references/knowledge/source")
ENCODINGS = ("cp866", "cp437", "cp850")


def demojibake(name: str) -> str | None:
    for enc in ENCODINGS:
        try:
            fixed = name.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != name:
            return fixed
    return None


def main() -> None:
    if not ROOT.is_dir():
        print(f"not found: {ROOT}")
        return
    changed = 0
    for p in sorted(ROOT.iterdir()):
        if p.name.isascii():
            continue
        fixed = demojibake(p.name)
        print(f"{p.name!r}  ->  {fixed!r}")
        if fixed and fixed != p.name:
            target = ROOT / fixed
            if target.exists():
                print("    (目标已存在，跳过)")
                continue
            p.rename(target)
            changed += 1
            print("    renamed OK")
    print(f"\n共重命名 {changed} 个目录")
    print("最终内容:")
    for p in sorted(ROOT.iterdir()):
        n = len(list(p.iterdir())) if p.is_dir() else 0
        print(f"  {p.name}  ({n} 文件)")


if __name__ == "__main__":
    main()
