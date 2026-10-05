"""从 pa-analysis 的 scripts/ 里提取「属于知识而非计算」的常量，转成 markdown（方案 T11）。

**必须在删 scripts/ 之前跑**，否则知识一起没了。

用 `ast` 提取字面量而不 import：这些脚本依赖外部环境，import 会失败；
而我们要的只是模块级常量的值。
"""
from __future__ import annotations

import ast
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "skills-src" / "pa-analysis"
SCRIPTS = BASE / "scripts"
REF = BASE / "references"


def _ev(node: ast.AST):
    """比 `literal_eval` 宽一点：认 `dict(...)` / `frozenset({...})` 这类构造调用。

    实测 `TRADE_INTENT` 写成 `dict(order=..., dir=...)`、枚举写成 `frozenset({...})`，
    纯 `literal_eval` 会全部失败 —— 那正是「知识被漏掉」的典型来源。
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        pass
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = node.func.id
        if fn == "dict":
            return {kw.arg: _ev(kw.value) for kw in node.keywords if kw.arg}
        if fn in ("frozenset", "set", "list", "tuple"):
            v = _ev(node.args[0]) if node.args else None
            if v is None:
                return None
            return sorted(v) if fn in ("list", "tuple") else v
    if isinstance(node, ast.Dict):
        return {_ev(k): _ev(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.List):
        return [_ev(e) for e in node.elts]
    if isinstance(node, ast.Set):
        return {_ev(e) for e in node.elts}
    if isinstance(node, ast.Tuple):
        return tuple(_ev(e) for e in node.elts)
    return None


def literals(path: Path) -> dict[str, object]:
    """取模块级常量的值（含 dict()/frozenset() 构造）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                v = _ev(node.value)
                if v is not None:
                    out[t.id] = v
    return out


def md_table(rows: list[tuple], header: tuple) -> str:
    s = "| " + " | ".join(header) + " |\n"
    s += "|" + "|".join("---" for _ in header) + "|\n"
    for r in rows:
        s += "| " + " | ".join("" if c is None else str(c) for c in r) + " |\n"
    return s


def gen_sequence_vocabulary() -> Path:
    L = literals(SCRIPTS / "signal_eval.py")
    mb = L.get("SEQUENCE_MIN_BARS") or {}
    rows = sorted(((k, v) for k, v in mb.items()), key=lambda x: (-(x[1] if isinstance(x[1], int) else 0), x[0]))
    out = (
        "# 信号序列词表（63 条）\n\n"
        "> 从节点3 的 `signal_eval.py` 提取的 `SEQUENCE_MIN_BARS`。**形态类条件里的 sequence 名"
        "必须取自本表**（与执行侧枚举逐字一致），写错会被拒。\n"
        "> `min_bars` 是该序列判定所需的最少 K 线根数 —— 数据不足时不要引用该序列。\n\n"
        + md_table(rows, ("sequence", "min_bars"))
        + f"\n共 {len(rows)} 条。\n"
    )
    p = REF / "sequence-vocabulary.md"
    p.write_text(out, encoding="utf-8")
    return p


def gen_signal_intent() -> Path:
    L = literals(SCRIPTS / "entry_playbook.py")
    ti = L.get("TRADE_INTENT") or {}
    rows = []
    for k in sorted(ti):
        v = ti[k]
        if isinstance(v, dict):
            rows.append((k, v.get("direction") or v.get("side") or "",
                         v.get("order") or v.get("order_type") or "",
                         v.get("note") or v.get("desc") or ""))
        else:
            rows.append((k, str(v), "", ""))
    out = (
        "# 信号 → 订单意图对照表（63 条）\n\n"
        "> 从节点3 的 `entry_playbook.py` 提取的 `TRADE_INTENT`。写证据链时用它确认"
        "「这个信号对应什么订单意图」，**但它不替代你的判断** —— 方案仍由你产出。\n\n"
        + md_table(rows, ("sequence", "direction", "order", "note"))
        + f"\n共 {len(rows)} 条。\n"
    )
    p = REF / "signal-intent-matrix.md"
    p.write_text(out, encoding="utf-8")
    return p


def gen_contract_enums() -> Path:
    L = literals(SCRIPTS / "validate_report.py")
    out = ["# 契约枚举（交付形态的取值域）\n",
           "> 从 `validate_report.py` 提取的枚举常量。用于自检方案字段是否落在合法取值域内。\n"]
    for name, title in [("TOP_FIELDS", "计划顶层字段"), ("SIGNAL_BAR_PATTERNS", "信号棒形态"),
                        ("LIFECYCLE_STAGES", "交易生命周期阶段"),
                        ("FORM_PARAM_TYPES", "形态条件参数类型")]:
        v = L.get(name)
        if v is None:
            continue
        out.append(f"\n## {title}（`{name}`）\n")
        if isinstance(v, (list, tuple, set)):
            items = sorted(v, key=str)
            out.append(f"共 {len(items)} 项：\n")
            for i in items:
                out.append(f"- `{i}`")
            out.append("")
        elif isinstance(v, dict):
            out.append(md_table(sorted(v.items(), key=lambda x: str(x[0])), ("key", "value")))
        else:
            out.append(f"```\n{v}\n```\n")
    p = REF / "contract-enums.md"
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p


def gen_delivery_gates() -> Path:
    """`validate_report.py` 的模块 docstring 就是 44 项门禁的完整说明。"""
    tree = ast.parse((SCRIPTS / "validate_report.py").read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree) or ""
    out = (
        "# 交付门禁（44 项）\n\n"
        "> 从 `validate_report.py` 的模块说明提取。**bot 不产出报告，所以这些门禁不会在交付时"
        "自动跑** —— 它的价值在于：它逐条写明了「一个完整方案必须自洽到什么程度」，"
        "你可以把它当作**方案自检清单**用（尤其 25 链式不变式、28 RR 自洽、30 失效对应、"
        "33 时间口径、34 形态词表）。\n\n"
        "```\n" + doc.strip() + "\n```\n"
    )
    p = REF / "delivery-gates.md"
    p.write_text(out, encoding="utf-8")
    return p


def main() -> int:
    made = [gen_sequence_vocabulary(), gen_signal_intent(), gen_contract_enums(),
            gen_delivery_gates()]
    for p in made:
        print("  %-34s %6d 字符" % (p.name, len(p.read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
