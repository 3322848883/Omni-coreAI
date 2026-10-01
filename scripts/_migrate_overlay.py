"""T3 迁移：基线 enabled 归一 + 生成本地 overlay（保留当前启用集）。

- config/bots/*.yaml  → enabled: false（安全基线）
- config/bots.local/*.yaml → 记录本机原本启用的 bot（环境差异）
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOTS = ROOT / "config" / "bots"
OVERLAY = ROOT / "config" / "bots.local"

ENABLED_RE = re.compile(r"^enabled:\s*true\s*$", re.MULTILINE)
ENABLED_ANY_RE = re.compile(r"^enabled:\s*\S+\s*$", re.MULTILINE)


def main() -> None:
    OVERLAY.mkdir(parents=True, exist_ok=True)
    enabled_now: list[str] = []

    for p in sorted(BOTS.glob("*.yaml")):
        if p.name.startswith("_"):
            continue  # 示例文件不动
        text = p.read_text(encoding="utf-8")
        was_enabled = bool(ENABLED_RE.search(text))
        if was_enabled:
            enabled_now.append(p.stem)
        # 基线统一为 false
        if ENABLED_ANY_RE.search(text):
            text = ENABLED_ANY_RE.sub("enabled: false", text, count=1)
        else:
            # 没有 enabled 键 → 在 bot_id 后插入
            text = re.sub(r"^(bot_id:.*)$", r"\1\nenabled: false", text, count=1, flags=re.MULTILINE)
        p.write_text(text, encoding="utf-8")

    # 生成本地 overlay
    for name in enabled_now:
        ov = OVERLAY / f"{name}.yaml"
        ov.write_text(
            f"# 本机 overlay（gitignore）：启用 {name}\n"
            f"# 基线 config/bots/{name}.yaml 为 enabled: false\n"
            f"enabled: true\n",
            encoding="utf-8",
        )

    print(f"基线已归一为 enabled: false（{len(list(BOTS.glob('*.yaml')))} 个文件）")
    print(f"本地 overlay 生成 {len(enabled_now)} 个:")
    for n in enabled_now:
        print("  ", n)


if __name__ == "__main__":
    main()
