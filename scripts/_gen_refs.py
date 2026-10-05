"""生成 price-action-trading 的三个新 references（方案 T7）。

- `references/00-core-steps.md`   26 主干步骤 + workflow×strategy 映射表（源自 SOUL）
- `references/06-rules-index.md`  规则条文速查（源自 strategy_workflow 的第6/7/8/11/12章 + 附录B + 第24章）
- `references/07-pitfalls-bans.md` 陷阱与禁止清单（源自 skill-pitfalls + workflow 附录D）

只做搬运与清洗，不改写内容；输出后校验每份 ≤12,000 字符。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "skills-src" / "price-action-trading"
REF = BASE / "references"
KNOW = REF / "knowledge"
LIMIT = 12000


def _lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines()


def _slice(lines: list[str], a: int, b: int) -> str:
    return "\n".join(lines[a - 1:b - 1])


def _clean(text: str) -> str:
    """去掉指向已不存在的路径的引用（source/ 已移出技能、memory/ 从不存在）。"""
    text = re.sub(r"memory/[\w/*]+\.md", "（本轮记忆：见订单上下文与决策日志）", text)
    text = re.sub(r"memory/\*?\.?md?", "（本轮记忆）", text)
    text = re.sub(r"source/V\d_B\d+", "原文（已移出技能，见仓库 docs/pa-source/）", text)
    text = text.replace("不要写死个人用户目录。", "")
    return text


def gen_core_steps() -> Path:
    p1 = REF / "SOUL_part1.md"
    p2 = REF / "SOUL_part2.md"
    src = p1.read_text(encoding="utf-8") + "\n" + p2.read_text(encoding="utf-8")
    # 26 步主干定义
    m = re.search(r"### 26步全部执行.*?(?=\n#{2,3} )", src, re.S)
    steps = m.group(0).strip() if m else ""
    # 步骤 → 映射表
    m2 = re.search(r"## 步骤 → workflow × strategy_workflow 配合映射.*?(?=\n## )", src, re.S)
    mapping = m2.group(0).strip() if m2 else ""
    # 执行规范（强制）
    m3 = re.search(r"## 执行规范（强制）.*?(?=\n## )", src, re.S)
    norms = m3.group(0).strip() if m3 else ""

    out = (
        "# 26 主干步骤与映射表\n\n"
        "> 本文件是 bot 每轮要走的**主干步骤**定义，以及每个 Step 该查哪一份知识。\n"
        "> `workflow.md` 的 53 个细粒度步骤是它的展开层；两套编号并存，"
        "**执行顺序以本文的主干步骤为准**。\n\n"
        + _clean(steps) + "\n\n"
        + _clean(mapping) + "\n\n"
        + _clean(norms) + "\n"
    )
    p = REF / "00-core-steps.md"
    p.write_text(out, encoding="utf-8")
    return p


def gen_rules_index() -> Path:
    sw = _lines(KNOW / "strategy_workflow.md")
    blocks = [
        ("入场信号优先级表（第6章）", 680, 720),
        ("止损规则表（第7章）", 829, 863),
        ("出场规则表（第8章）", 863, 892),
        ("交易者方程式速查表（第11章）", 929, 1021),
        ("禁止交易清单（第12章）", 1021, 1075),
        ("规则引擎快速索引（附录B）", 1096, 1126),
        ("交易执行规则引擎（第24章）", 1937, 2011),
    ]
    out = (
        "# 规则条文速查（bot 每轮用）\n\n"
        "> 从 `strategy_workflow.md` 抽出的**规则条文**：入场优先级、止损、出场、"
        "交易者方程、禁止清单、执行硬约束。需要更细的决策树时再读 `strategy_workflow.md` 全文"
        "（它超过单次读取上限，需按章分次读）。\n\n"
    )
    for title, a, b in blocks:
        out += f"## {title}\n\n" + _slice(sw, a, b).strip() + "\n\n---\n\n"
    p = REF / "06-rules-index.md"
    p.write_text(out, encoding="utf-8")
    return p


def gen_pitfalls() -> Path:
    pit = (REF / "skill-pitfalls.md").read_text(encoding="utf-8")
    wf = _lines(KNOW / "workflow.md")
    m = re.search(r"### 附录D：禁止事项清单.*?(?=\n### )", "\n".join(wf), re.S)
    bans = m.group(0).strip() if m else ""
    out = (
        "# 陷阱与禁止清单\n\n"
        "> 已知的分析陷阱（来自实战复盘）+ 禁止事项清单。**出手前扫一眼**。\n\n"
        + _clean(pit) + "\n\n---\n\n" + _clean(bans) + "\n"
    )
    p = REF / "07-pitfalls-bans.md"
    p.write_text(out, encoding="utf-8")
    return p


def main() -> int:
    made = [gen_core_steps(), gen_rules_index(), gen_pitfalls()]
    bad = 0
    for p in made:
        n = len(p.read_text(encoding="utf-8"))
        flag = "OK" if n <= LIMIT else "超限!"
        if n > LIMIT:
            bad += 1
        print("  %-34s %6d 字符  %s" % (p.name, n, flag))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
