#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按币审计：覆盖率 · 缺 symbol 率 · 越界率 · 自动补全率 · 每币取数次数 · 图清单。

方案的 S2.6-2 把「运行期守卫」写成了常驻脚本（`scripts/_symbol_audit.py`），但一直
没落地 —— 于是"多币到底跑成什么样"只能靠临时探针，结论无法复现、也无从比较。
本脚本把那些口径固定下来，数据源是**已有的**落盘产物，不需要重跑 LLM：

  data/bots/<id>/state/*.thinking.json   每轮的 tool_usage / tool_usage_summary / charts
  data/bots/<id>/state/memory_journal.jsonl   每轮的 symbols / decisions（决策覆盖率）

口径（与方案的 S2.8 断言逐条对应）：

  覆盖率          = 该轮**取到数**的币 / 宇宙大小。三个口径分开报，别混：
                    `tool`（模型调了工具）、`decision`（journal 里有结论）、`chart`（有图）
  缺 symbol 率    = `missing_symbol` / 带 symbol 的调用数 —— 多币下**必须为 0**
  越界率          = `out_of_universe` / 总调用 —— 多币下**必须为 0**
  自动补全率      = `symbol_note == auto_single` / 带 symbol 的调用数（单币≈100% 属正常）
  每币取数次数    = `by_symbol` 逐币累加
  图清单          = `charts` 的 {symbol, timeframe, ok, bytes}

用法::

    python scripts/_symbol_audit.py --bot ab-multi-new
    python scripts/_symbol_audit.py --bot brooks-btc --bot ladder-eth --last 20
    python scripts/_symbol_audit.py --bot ab-multi-new --json
    python scripts/_symbol_audit.py --bot ab-multi-new --strict   # 违反不变量 → 退出码 1

`--strict` 判据（多币宇宙才检查后两条）：缺 symbol 率 > 0 / 越界率 > 0。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omnialpha.strategist.symbols import AUTO_SINGLE  # noqa: E402


def _load_thinking(paths: list[Path]) -> list[tuple[str, dict]]:
    out = []
    for p in paths:
        try:
            out.append((p.name, json.loads(p.read_text(encoding="utf-8"))))
        except Exception as e:  # noqa: BLE001 — 审计不能因一个坏文件整体失败
            print(f"  ! 跳过坏文件 {p.name}: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def _universe(root: Path, bot_id: str) -> list[str]:
    """宇宙取自**配置**（而不是从产出里反推）—— 否则"一个币都没分析"会看不出来。"""
    try:
        from omnialpha.config import load_bot_config
        from omnialpha.watcher import ProjectPaths

        bot = load_bot_config(ProjectPaths(root).config_dir / f"{bot_id}.yaml")
        return [str(s) for s in (getattr(bot, "symbols", None) or [])]
    except Exception as e:  # noqa: BLE001
        print(f"  ! 读不到 {bot_id} 的配置（宇宙按空处理）: {e}", file=sys.stderr)
        return []


def audit_bot(root: Path, bot_id: str, last: int) -> dict:
    st = root / "data" / "bots" / bot_id / "state"
    universe = _universe(root, bot_id)
    tfiles = sorted(st.glob("*.thinking.json"))[-last:]
    rounds = _load_thinking(tfiles)

    journal: list[dict] = []
    jf = st / "memory_journal.jsonl"
    if jf.exists():
        for ln in jf.read_text(encoding="utf-8").splitlines()[-last:]:
            if ln.strip():
                try:
                    journal.append(json.loads(ln))
                except Exception:  # noqa: BLE001
                    pass

    tool_cov, dec_cov, chart_cov = [], [], []
    per_symbol: Counter = Counter()
    missing = oou = sym_calls = total_calls = autofill = 0
    charts_all: list[dict] = []
    for _name, d in rounds:
        s = d.get("tool_usage_summary") or {}
        by = s.get("by_symbol") or {}
        got = {k for k in by if k}
        tool_cov.append((len(got), sorted(got)))
        per_symbol.update({k: int(v.get("calls") or 0) for k, v in by.items()})
        missing += int(s.get("missing_symbol") or 0)
        oou += int(s.get("out_of_universe") or 0)
        total_calls += int(s.get("total_calls") or 0)
        sym_calls += sum(int(v.get("calls") or 0) for v in by.values())
        for e in (d.get("tool_usage") or []):
            if isinstance(e, dict) and str(e.get("symbol_note") or "") == AUTO_SINGLE:
                autofill += 1
        csyms = {c.get("symbol") for c in (d.get("charts") or []) if c.get("symbol")}
        chart_cov.append(len(csyms))
        charts_all.extend(d.get("charts") or [])

    # 决策覆盖率：journal 的 symbols（每轮对几个币下了结论）
    for r in journal:
        syms = {s for s in (r.get("symbols") or []) if s}
        if syms:
            dec_cov.append(len(syms))

    n = len(rounds)
    un = len(universe)

    def _rate(x, y):
        return (x / y) if y else 0.0

    report = {
        "bot": bot_id,
        "universe": universe,
        "rounds": n,
        "journal_rounds": len(journal),
        "coverage_tool": [c[0] for c in tool_cov],
        "coverage_decision": dec_cov,
        "coverage_chart": chart_cov,
        "universe_size": un,
        "tool_calls_total": total_calls,
        "symbol_calls": sym_calls,
        "missing_symbol": missing,
        "out_of_universe": oou,
        "auto_filled": autofill,
        "missing_symbol_rate": _rate(missing, sym_calls),
        "out_of_universe_rate": _rate(oou, total_calls),
        "auto_fill_rate": _rate(autofill, sym_calls),
        "per_symbol_calls": dict(per_symbol),
        "charts": charts_all,
        "charts_ok": sum(1 for c in charts_all if c.get("ok")),
        "multi_symbol": un > 1,
    }
    if n:
        report["coverage_tool_avg"] = sum(c[0] for c in tool_cov) / n
        report["coverage_full_rounds"] = sum(1 for c in tool_cov if c[0] == un and un)
    return report


def _fmt(r: dict) -> str:
    L = []
    A = L.append
    A("=" * 74)
    A(f"bot={r['bot']}  宇宙({r['universe_size']})={r['universe']}")
    A(f"轮数: thinking={r['rounds']}  journal={r['journal_rounds']}")
    if not r["rounds"]:
        A("  (没有 thinking.json —— 这个 bot 没跑过 plan，或产物被清过)")
    A("-" * 74)
    A(f"覆盖率   tool={r['coverage_tool']}  平均={r.get('coverage_tool_avg', 0):.1f}/{r['universe_size']}"
      f"  全覆盖轮={r.get('coverage_full_rounds', 0)}/{r['rounds']}")
    A(f"         decision={r['coverage_decision']}")
    A(f"         chart={r['coverage_chart']}  图={len(r['charts'])} 张，成功={r['charts_ok']}")
    A(f"缺 symbol 率 = {r['missing_symbol']}/{r['symbol_calls']} = {r['missing_symbol_rate']:.2%}")
    A(f"越界率      = {r['out_of_universe']}/{r['tool_calls_total']} = {r['out_of_universe_rate']:.2%}")
    A(f"自动补全率  = {r['auto_filled']}/{r['symbol_calls']} = {r['auto_fill_rate']:.2%}")
    A(f"每币取数    = {r['per_symbol_calls']}")
    if r["charts"]:
        A("图清单:")
        for c in r["charts"][:24]:
            A(f"  {c.get('symbol')} {c.get('timeframe')} ok={c.get('ok')} bytes={c.get('bytes')}")
        if len(r["charts"]) > 24:
            A(f"  … 另 {len(r['charts']) - 24} 张")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="按币审计（覆盖率/缺 symbol 率/越界率/图清单）")
    ap.add_argument("--bot", action="append", required=True, help="bot_id，可重复")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--last", type=int, default=20, help="只看最近 N 轮（默认 20）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true",
                    help="多币宇宙下缺 symbol 率或越界率 > 0 → 退出码 1")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    reports = [audit_bot(root, b, args.last) for b in args.bot]

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for r in reports:
            print(_fmt(r))
            print()

    if args.strict:
        bad = []
        for r in reports:
            if not r.get("multi_symbol"):
                continue
            if r["missing_symbol_rate"] > 0:
                bad.append(f"{r['bot']}: 缺 symbol 率 {r['missing_symbol_rate']:.2%} > 0")
            if r["out_of_universe_rate"] > 0:
                bad.append(f"{r['bot']}: 越界率 {r['out_of_universe_rate']:.2%} > 0")
        if bad:
            print("STRICT FAIL:")
            for b in bad:
                print("  -", b)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
