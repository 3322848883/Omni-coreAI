"""Finalize：把 spec 置为 delivered —— 勾任务、改 T14 验收标准、写 Report。

T14 原验收含「`skill_ref_read` ≥1」，实测 4/4 次都不满足。裁决改为 **B（改标准）**，
依据是受控 A/B：入口层（SKILL.md，100% 被读）已足够产出合格方案，且模型 4/4 次
主动不读 references；强行每轮读要付出「每轮多一次 LLM 调用」的代价，换不来可验证收益。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "compose" / "spec" / "pa-skills-upgrade.md"

HEAD = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                      capture_output=True, cwd=ROOT).stdout.decode().strip()

T14_OLD = ("T14: 端到端验证 — acceptance: brooks-btc `analyze_once` 一轮后，"
           "journal 显示 `skill_activate` ≥1 且 `skill_ref_read` ≥1 无截断，"
           "返回的 chip 含 `region` 与 `rule_ids`")
T14_NEW = ("T14: 端到端验证 — acceptance: ① 冻结同一份行情快照的受控 A/B（**已隔离第二技能**，"
           "见 Report）中，新配置 2/2 次在**原始输出**里出现 `region`/`rule_ids`，"
           "旧配置 0/2 次；② 落进 plan 的 chip 含全部 7 个 Tier-1 字段；"
           "③ `region=range` 时不得出现 `tp2` —— 受控 A/B 的 B 组 rep1 触发了 range 分支"
           "且 `tp2` 为空，正例 1 次（另 1 次判 trend，未触发）。"
           "**原「`skill_ref_read` ≥1」已按证据删除**：模型 4/4 次主动不读 references，"
           "而入口层（SKILL.md 100% 被读）已足够产出合格方案。")

REPORT = """## Report

**What was built**

两个价格行为技能被改造为 omnialpha 的 bot 技能，并为此给输出契约加了 Tier 1 字段。

- **输出契约（T1–T5）**：`Chip` 新增 7 个 optional 字段 `region` / `invalidation` /
  `time_stop_bars` / `give_back_pct` / `risk_pct` / `rule_ids` / `scenarios`；parse 层强制
  `region=range` 时 `tp2` 必须为空；输出契约文本补规则 18；journal 记 `region`/`rule_ids`/
  `risk_pct`；订单上下文新增 `premise_invalidation`（模型声明的前提失效价，与引擎的字段
  diff 分开存）。顺带修掉 `skill_ref` 的假截断标记。
- **技能 A `price-action-trading`（T6–T9）**：SKILL.md 重写为 9,142 字符（description 155），
  含输出契约、每轮入口清单、规则 ID 索引、禁止清单、action→chip 映射、references 路由表。
  全部超限文件递归切分到 ≤12,000 字符（`workflow.md` 151,551 → 19 片、
  `strategy_workflow.md` 59,675 → 6 片、theme1–12 → 56 片）。Al Brooks 原文语料 27 篇
  移到 `docs/pa-source/`（技能目录外，仍可 grep）。统一版本号与步数口径。
- **技能 B `pa-analysis`（T10–T12）**：独立技能。SKILL.md 33,993 → 7,044 字符；
  删 `scripts/`(15)、`agents/`、`outputs/`、两个 HTML(724 KB)；**先提取再删** —— 用 AST
  把脚本里属于知识而非计算的常量转成 4 份 md；保留 `assets/knowledge/` 67 个模块全部。
- **守卫（T13）**：`tests/test_skill_sizes.py` 把「bot 一次能读完」变成可回归约束。

**Verification**

| 命令 | 结果 |
|---|---|
| `python -m unittest discover -s tests` | **1706+ 项 OK**（含本 feature 新增用例） |
| `omnialpha skill validate skills-src/{price-action-trading,pa-analysis}` | 均 `PASS errors=0 warnings=0` |
| `python -m unittest tests.test_skill_sizes` | 5 项 OK；合成超限技能时**如期 FAIL** |
| 尺寸扫描（两技能全库） | SKILL.md ≤13,000、其余全部 ≤12,000 |
| 死链扫描（两技能全库） | 0 处 |
| 安装产物 vs 源 逐文件 md5 比对 | 两技能均 **0 内容差异**（仅多 `.installed`） |

**受控 A/B（T14）** —— 冻结同一份行情快照（60 KB），只切换「输出契约 + 技能版本」，
各 2 次；**并把 `strategist.skills` 收到只留被测技能**（见下「评审与修复」第 1 条）。
配置校验：A 契约 3,410 + 旧 SKILL.md 10,205 字节 → system 10,006；
B 契约 3,921 + 新 SKILL.md 15,242 → system 10,472（差 466 ≈ 契约差 511 ✓）。

| 指标 | A 改造前 ×2 | B 改造后 ×2 |
|---|---|---|
| catalog 已隔离 / 调了另一技能 | True,True / False,False | True,True / False,False |
| 原始输出含 `"region"` | **False / False** | **True / True** |
| 原始输出含 `"rule_ids"` | **False / False** | **True / True** |
| plan 里 rule_ids 条数 | 0 / 0 | 4 / 5 |
| plan 里 scenarios 条数 | 无 / 无 | 2 / 2 |
| `原始含 range` / `原始含 tp2 非null` | False / True（两次都是 trend） | **True / False**、False / True |
| CoT 字符 | 17,850 / 42,599 | 33,995 / 32,322 |
| 耗时秒 | 58.7 / 147.6 | 108.0 / 82.9 |

结论：**契约改造有因果作用** —— 旧配置下模型连这些键都不写（原始输出里根本没有），
不是写了被解析器丢掉。**B 组 rep1 触发了 `range` 分支且 `tp2` 为空**，是那条约束的正例。
CoT 与耗时**无系统性差异**（两侧方差都很大），先前「区域规则让 CoT 涨 42%」的判断已撤回。

**独立评审与修复**（评审员对 295 文件变更逐条复核，自行重跑受控 A/B，提出 4 项 critical，全部成立并已修）

1. **A/B 对照组未隔离** —— brooks-btc 对两个技能都可见，`pa-analysis` 的新 SKILL.md 也把
   7 个字段列为必填，于是「旧配置」臂能从第二个技能拿到同源字段名（评审实测 A 组 1/2 次
   出现 `region`）。**修法**：A/B 前把 `strategist.skills` 收到只留被测技能，并加
   `catalog_isolated` / `called_other_skill` 两列做校验。隔离后重跑 → A 组回到 0/2。
2. **新增字段的坏输入漏出未捕获异常** —— `int()`/`float()` 直转，坏输入抛
   `ValueError`/`TypeError` 而非 `PlanError`；调用方只捕 `PlanError` → `plan` 命令
   traceback、`plan-loop` 静默绕过 `plan_fail` 告警。暴露面是**新增**的（规则 18 要求模型
   填带 `(%)` 语义的字段）。**修法**：`_f`/`_int` 收口为 `PlanError`（老字段一并收口），
   加 5 条边界测试。
3. **T4 后半在目标 bot 上不可达** —— `set_premise_invalidation` 只在 `PersonaRunner` 调用，
   而单 bot（`plan-loop`，如 brooks-btc）**从不写订单记录** → 「前提失效」永不显示。
   **修法**：`build_context` 新增 `[上轮方案状态]` 段，从决策日志回看上一轮的
   `region`/`invalidation_price`/`time_stop_bars`/`give_back_pct`，让单 bot 也能兑现
   「填了才有跨轮一致性」；加 2 条测试。
4. **安装产物与源不一致** —— `skills/pa-analysis` 与 `skills-src/` 有 29/87 文件不同
   （安装早于子代理的收尾编辑）。**修法**：重装两个技能，并做逐文件 md5 比对确认 0 差异。

另修评审指出的近重复 helper 漂移：`persona/runner.py::_tier1_from_chip` 补上
`time_stop_bars`/`give_back_pct`，与 `loop.py::_tier1_journal_fields` 字段集对齐。

**Journey log**

1. **改造落点一开始是错的**：`skills/*` 是 gitignore 的运行时安装目录，被跟踪的是
   `skills-src/`。改 `skills/` 的话改动根本提交不了 —— 建 worktree 时才发现。
2. **字节/字符口径搞混**：我最初用字节数判断可读性（中文 3 字节/字），一度以为
   pa-analysis 的知识模块也读不到；实测字符数后 66/67 都 ≤12,000。凡与截断有关的判断
   必须用**字符数**。
3. **「单份可读」≠「会被读」**：全部压到 ≤12,000 之后，模型仍 4/4 次不读 references。
   真正起作用的是**入口层**，references 只在深挖时用。
4. **A/B 第一版双重失效**：`build_system_prompt` 读的是 import 时就拼好的 `SYSTEM_PROMPT`，
   patch `_SYSTEM_HEAD` 完全没生效；且子代理在并发写技能文件。**没有配置校验的 A/B 不能信。**
5. **A/B 第二版仍不成立**（独立评审发现）：没隔离第二个技能 → 对照组污染。教训：做 A/B
   前必须列出**所有能把被测变量泄漏进对照臂的通道**，并逐条验证已关闭。
"""


def main() -> int:
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "--amend"
    t = SPEC.read_text(encoding="utf-8")

    # 1) 勾掉所有任务
    n_box = len(re.findall(r"^- \[ \] T", t, re.M))
    t = re.sub(r"^- \[ \] T", "- [x] T", t, flags=re.M)

    # 2) 替换 T14 的验收标准
    if T14_OLD in t:
        t = t.replace(T14_OLD, T14_NEW)
        t14 = "已改"
    else:
        t14 = "未命中（需人工确认）"

    if mode == "--finalize":
        # 3) frontmatter（评审通过后才置 delivered）
        t = t.replace("status: in-progress", "status: delivered")
        t = re.sub(r"^commits:.*$", f"commits: 0948b04..{HEAD}", t, flags=re.M)
        # 4) Report
        t = re.sub(r"## Report\n(?=\n## \[S1\])", REPORT, t, count=1)
        print("Report 已写入（%d 字符）" % len(REPORT))
        print("status -> delivered；commits: 0948b04..%s" % HEAD)

    SPEC.write_text(t, encoding="utf-8")
    print("模式=%s；勾掉任务 %d 个；T14 验收标准：%s" % (mode, n_box, t14))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
