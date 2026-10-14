#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 `docs/config-reference.md`（Bot 配置键白名单的文档化）。

**为什么要有这个脚本**（2026-10-14 全面检查 N13）：文档与配置此前**无联动** ——
白名单加了键、文档不更新，或文档写了键、代码里没人读，都不会有测试失败。这里把
文档变成**生成物**：改 `omnialpha/config_keys.py` → 重跑本脚本 → 提交；
`tests/test_config_reference_fresh.py` 会检测漂移，让文档无法腐化。

跑法：
    python scripts/gen_config_reference.py          # 写入文档
    python scripts/gen_config_reference.py --check  # 只检查是否漂移（CI 用）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config_keys import DESCRIPTIONS, SCHEMA  # noqa: E402

DOC = ROOT / "docs" / "config-reference.md"


def render() -> str:
    """渲染完整文档（**纯函数**：同样的白名单必得同样的文本）。"""
    lines = [
        "# Bot 配置键参考",
        "",
        "> ⚙️ **本文件自动生成，请勿手改。**",
        "> 来源：`omnialpha/config_keys.py`；生成：`python scripts/gen_config_reference.py`。",
        "> `tests/test_config_reference_fresh.py` 会检测漂移。",
        "",
        "白名单的用途：加载 bot 配置时，**白名单之外的键会告警**（阶段 1 只 WARN、不报错）。",
        "原因是「配了但没人读」的键此前**完全静默** —— 实测先例：5 个 `ab-*` bot 配了",
        "`account_risk.auto_protect: \"dry\"` 却没有 `auto_protect_sl_pct`，而",
        "`ensure_protection` 在 `pct <= 0` 时直接早退，dry 观察期其实什么都没观察到。",
        "",
        "启用 bot 请写 `config/bots.local/<同名>.yaml`（基线里的 `enabled` 一律忽略）。",
        "",
        "---",
        "",
    ]
    for path in sorted(SCHEMA, key=lambda p: (p.count("."), p)):
        keys = sorted(SCHEMA[path])
        title = path or "<顶层>"
        lines.append(f"## `{title}`")
        lines.append("")
        desc = DESCRIPTIONS.get(path)
        if desc:
            lines.append(desc)
            lines.append("")
        lines.append(f"共 **{len(keys)}** 个键：")
        lines.append("")
        lines.extend(f"- `{k}`" for k in keys)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    text = render()
    if check:
        current = DOC.read_text(encoding="utf-8") if DOC.is_file() else ""
        if current != text:
            print("DRIFT: docs/config-reference.md 与 config_keys 白名单不一致；"
                  "请重跑 python scripts/gen_config_reference.py")
            return 1
        print("OK: docs/config-reference.md 与白名单一致")
        return 0
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(text, encoding="utf-8")
    print(f"wrote {DOC.relative_to(ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
