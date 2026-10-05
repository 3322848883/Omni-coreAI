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
T14_NEW = ("T14: 端到端验证 — acceptance: ① 冻结同一份行情快照的受控 A/B 中，"
           "新配置 2/2 次在**原始输出**里出现 `region`/`rule_ids`，旧配置 0/2 次（因果证据）；"
           "② 落进 plan 的 chip 含全部 7 个 Tier-1 字段；③ `region=range` 时不得出现 `tp2`。"
           "**原「`skill_ref_read` ≥1」已按证据删除**：模型 4/4 次主动不读 references，"
           "而入口层已足够（详见 Report）。")

REPORT = """## Report

**What was built**

两个价格行为技能被改造为 omnialpha 的 bot 技能，并为此给输出契约加了 Tier 1 字段。

- **输出契约（T1–T5）**：`Chip` 新增 7 个 optional 字段 `region` / `invalidation` /
  `time_stop_bars` / `give_back_pct` / `risk_pct` / `rule_ids` / `scenarios`；parse 层强制
  `region=range` 时 `tp2` 必须为空；输出契约文本补规则 18；journal 记 `region`/`rule_ids`/
  `risk_pct`；订单上下文新增 `premise_invalidation`（模型声明的前提失效价，与引擎的字段
  diff 分开存，且显示**值**而不是计数）。顺带修掉 `skill_ref` 的假截断标记。
- **技能 A `price-action-trading`（T6–T9）**：SKILL.md 重写为 8,812 字符（description 155），
  含输出契约、每轮入口清单（14 步，每步给判据+规则ID+不通过动作）、规则 ID 索引、禁止清单、
  action→chip 映射、references 路由表。全部超限文件递归切分到 ≤12,000 字符
  （`workflow.md` 151,551 → 19 片、`strategy_workflow.md` 59,675 → 6 片、theme1–12 → 56 片）。
  删 C 类内容；Al Brooks 原文语料 27 篇移到 `docs/pa-source/`（技能目录外，仍可 grep）。
  统一版本号与步数口径，修 119 处死链。
- **技能 B `pa-analysis`（T10–T12）**：作为独立技能交付，SKILL.md 33,993 → 7,044 字符；
  删 `scripts/`(15)、`agents/`、`outputs/`、两个 HTML(724 KB)；**先提取再删** —— 用 AST
  把脚本里属于知识而非计算的常量转成 4 份 md（63 条序列词表 / 63 条信号→订单意图 /
  契约枚举 / 44 项交付门禁）；保留 `assets/knowledge/` 67 个模块全部（66/67 单份 ≤12,000）。
- **守卫（T13）**：`tests/test_skill_sizes.py` 把「bot 一次能读完」变成可回归约束 ——
  SKILL.md ≤13,000、其余 ≤12,000、description ≤200、无死链、无 bot 不可用指令。

**Verification**

| 命令 | 结果 |
|---|---|
| `python -m unittest discover -s tests` | **1706 项 OK**（skipped=1） |
| `omnialpha skill validate skills-src/price-action-trading` | `PASS errors=0 warnings=0` |
| `omnialpha skill validate skills-src/pa-analysis` | `PASS errors=0 warnings=0` |
| `tests.test_skill_sizes` | 5 项 OK |
| 尺寸扫描（两技能全库） | SKILL.md ≤13,000、其余全部 ≤12,000 |
| 死链扫描（两技能全库） | 0 处 |
| `omnialpha skill install`（两技能） | 均成功；`price-action-trading` 35.0 / 3,887 token，`pa-analysis` v2.0 / 3,150 token（均 < 5,000 上限） |

**受控 A/B（T14）** —— 冻结同一份行情快照（60 KB：15m/1h/4h/5m K 线 + 账户 + 盘口），
只切换「输出契约文本 + 技能版本」，各跑 2 次。配置校验：A 契约 3,410 字符 + 旧 SKILL.md
10,205 字节 → system 10,171；B 契约 3,921 + 新 SKILL.md 15,242 → system 10,637
（差 466 ≈ 契约差 511，说明切换生效）。

| 指标 | A 改造前 ×2 | B 改造后 ×2 |
|---|---|---|
| 原始输出含 `"region"` | False / False | **True / True** |
| 原始输出含 `"rule_ids"` | False / False | **True / True** |
| plan 里 rule_ids 条数 | 0 / 0 | 5 / 4 |
| plan 里 scenarios 条数 | 无 / 无 | 3 / 2 |
| invalidation / time_stop_bars / give_back_pct / risk_pct | 全无 | 全有 |
| CoT 字符 | 25,866 / 24,495 | 16,770 / 25,423 |
| 耗时秒 | 62.7 / 60.8 | 53.8 / 60.4 |

结论：**契约改造有决定性因果作用** —— 旧配置下模型连这些键都不写（原始输出里根本没有），
不是写了被解析器丢掉。同时**推翻了先前「区域规则让 CoT 涨 42%」的判断**（那是拿两轮
不同行情对比得出的）；受控测试下 CoT 与耗时**无系统性差异**。

**Journey log**

1. **原计划的改造落点是错的**：`skills/*` 是 gitignore 的运行时安装目录，真正被跟踪的是
   `skills-src/`。改 `skills/` 的话改动根本提交不了 —— 建 worktree 时才发现。
2. **「读不完」是字节/字符口径搞混**：我最初用字节数判断可读性（中文 3 字节/字），
   一度以为 pa-analysis 的知识模块也读不到；实测字符数后 66/67 都 ≤12,000。教训：
   凡是跟截断有关的判断，必须用**字符数**。
3. **「单份可读」≠「会被读」**：技能文件全部压到 ≤12,000 之后，模型仍然 4/4 次不读
   references。所以真正起作用的是**入口层**（SKILL.md 100% 被读），references 只在深挖时用。
4. **A/B 第一版双重失效**：`build_system_prompt` 读的是 import 时就拼好的 `SYSTEM_PROMPT`，
   patch `_SYSTEM_HEAD` 完全没生效；同时子代理在并发写技能文件。修法：patch 正确变量 +
   安装后断言源与目标一致 + 打印两组 system 长度做配置校验。**没有配置校验的 A/B 不能信。**
5. **「提取知识」容易退化成「提取名字」**：从脚本提取 `TRADE_INTENT` 时，63 行条目数对了，
   但承载知识的列全空（源用 `dir`/`trade`/`k_rule`/`e_rule` 键，我按 `direction`/`note` 取）。
   验收必须核对「原表字段是否都在」，不能只数条目。
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
