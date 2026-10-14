#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一测试 runner：unittest + pytest 双跑 + **收集护栏**。

为什么需要（2026-10-14 全面检查 N2）：`tests/` 下曾有 2 个 pytest 风格文件
（`test_skill_force.py` 的类未继承 `TestCase`、`test_skill_required.py` 用
`pytest.mark.skipif`），`unittest` 收集到 **0 个测试** —— 文件在、却从不执行，
`requirements.txt` 里连 pytest 都没声明。这类「静默不跑」比测试失败更危险。

本 runner 做三件事：
  1. **收集护栏**：逐个 `tests/test_*.py` 用 unittest 加载，收集到 0 个测试即失败；
  2. 全量 unittest；
  3. 若装了 pytest，再跑一遍 pytest（覆盖 pytest 风格的 fixture/参数化）。

跑法：
    python scripts/run_tests.py            # 全量
    python scripts/run_tests.py --guard    # 只跑收集护栏（快）
退出码：0 = 全绿；非 0 = 有失败。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TESTS = ROOT / "tests"


def guard_collection() -> int:
    """每个 `tests/test_*.py` 必须被 unittest 收集到 >0 个测试。"""
    loader = unittest.TestLoader()
    bad: list[str] = []
    files = sorted(TESTS.glob("test_*.py"))
    for p in files:
        mod = f"tests.{p.stem}"
        try:
            suite = loader.loadTestsFromName(mod)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{p.name}: 加载失败 {e!r}")
            continue
        n = suite.countTestCases()
        if n == 0:
            bad.append(f"{p.name}: unittest 收集到 0 个测试（pytest 风格？见 N2）")
    if bad:
        print("收集护栏失败：")
        for b in bad:
            print(f"  ✗ {b}")
        return 1
    print(f"收集护栏通过：{len(files)} 个测试文件均被收集到 >0 个测试")
    return 0


def run_unittest() -> int:
    print("\n=== unittest（逐文件独立进程，规避跨文件顺序污染）===")
    py = sys.executable
    pass_n = fail_n = total = 0
    failed: list[str] = []
    for p in sorted(TESTS.glob("test_*.py")):
        out = subprocess.run([py, "-m", "unittest", f"tests.{p.stem}"],
                             cwd=str(ROOT), capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        text = (out.stdout or "") + (out.stderr or "")
        n = 0
        for line in text.splitlines():
            if line.startswith("Ran ") and " test" in line:
                try:
                    n = int(line.split()[1])
                except (IndexError, ValueError):
                    n = 0
        total += n
        if "OK" in text and "FAILED" not in text:
            pass_n += 1
        else:
            fail_n += 1
            failed.append(p.name)
    print(f"  PASS_FILES={pass_n} FAIL_FILES={fail_n} TOTAL_TESTS={total}")
    if failed:
        print(f"  失败文件：{failed}")
        return 1
    return 0


def run_pytest() -> int:
    try:
        import pytest  # noqa: F401
    except ImportError:
        print("\n=== pytest：未安装，跳过（pip install pytest）===")
        return 0
    print("\n=== pytest ===")
    rc = subprocess.call([sys.executable, "-m", "pytest", "-q", "tests"],
                         cwd=str(ROOT))
    return 0 if rc == 0 else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--guard", action="store_true", help="只跑收集护栏")
    args = ap.parse_args(argv)

    rc = guard_collection()
    if args.guard:
        return rc
    rc |= run_unittest()
    rc |= run_pytest()
    print("\n结论：" + ("全绿" if rc == 0 else "有失败"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
