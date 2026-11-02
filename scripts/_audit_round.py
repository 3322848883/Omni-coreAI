#!/usr/bin/env python3
"""一轮落地全方位审计：AI 意图 → 引擎决策 → 挂单 → 保护/止盈 → 一致性。

用法：
  python scripts/_audit_round.py --bot martin-grid-bi-gp --plan C:/tmp/audit_plan.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Audit:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, level: str, name: str, detail: str = ""):
        self.items.append((level, name, detail))

    def check(self, ok: bool, name: str, detail: str = "", warn_only: bool = False):
        self.add(PASS if ok else (WARN if warn_only else FAIL), name, detail)

    def report(self):
        order = {FAIL: 0, WARN: 1, PASS: 2}
        items = sorted(self.items, key=lambda x: order[x[0]])
        for lvl, name, detail in items:
            mark = {PASS: "OK  ", FAIL: "FAIL", WARN: "WARN"}[lvl]
            print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
        nf = sum(1 for i in items if i[0] == FAIL)
        nw = sum(1 for i in items if i[0] == WARN)
        print(f"\n  合计 {len(items)} 项：FAIL {nf} / WARN {nw} / PASS {len(items) - nf - nw}")
        return nf, nw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="martin-grid-bi-gp")
    ap.add_argument("--plan", default="C:/tmp/audit_plan.json")
    ap.add_argument("--sym", default="ETH_USDT")
    a = ap.parse_args()

    A = Audit()
    plan = json.load(open(a.plan, encoding="utf-8")) if os.path.exists(a.plan) else {}
    pm = plan.get("meta") or {}
    two_way = pm.get("two_way") or {}
    ai_levels = [float(x) for x in (pm.get("structure_levels") or [])]

    print("=" * 78)
    print("① AI 意图")
    print("=" * 78)
    orders = plan.get("orders") or []
    entry_actions = {"open_long", "open_short", "add_long", "add_short",
                     "stop_entry_long", "stop_entry_short"}
    bad_entry = [o for o in orders if str(o.get("action") or "").lower() in entry_actions]
    A.check(not bad_entry, "没有多余的入场腿（template_owns_entries 应跳过）",
            f"{len(bad_entry)} 条：{[o.get('action') for o in bad_entry]}" if bad_entry else "0 条")
    A.check(bool(two_way), "two_way 两侧都给", json.dumps(
        {k: {"lv": v.get("levels"), "step": v.get("step_pct"), "w": v.get("weight")}
         for k, v in two_way.items()}, ensure_ascii=False))
    A.check(5 <= len(ai_levels) <= 10, "结构位数量 5~10 个", f"{len(ai_levels)} 个")
    for side in ("long", "short"):
        n = int((two_way.get(side) or {}).get("levels") or 0)
        st = (two_way.get(side) or {}).get("step_pct")
        A.check(n <= 10, f"{side}.levels ≤ 10", f"{n}")
        A.check(st is None or 0.66 <= float(st) <= 2.2,
                f"{side}.step_pct 在 0.66~2.2", f"{st}")

    # ── 引擎决策 ────────────────────────────────────────────
    tp_line = open(os.path.join(ROOT, "data/bots", a.bot, "logs", "trades.jsonl"),
                   encoding="utf-8").read().strip().split("\n")[-1]
    trade = json.loads(tp_line)
    print("\n" + "=" * 78)
    print("② 引擎决策")
    print("=" * 78)
    sides = {}
    for s in trade.get("steps") or []:
        det = s.get("detail") or {}
        dyn = det.get("dynamic") or {}
        if not dyn:
            continue
        sd = dyn.get("side")
        so = dyn.get("structure_only") or {}
        legs = dyn.get("legs") or []
        sides[sd] = {"det": det, "dyn": dyn, "so": so, "legs": legs,
                     "step": float(det.get("step_pct") or 0)}
        kept = int(so.get("in_side") or 0) - int(so.get("deduped_out") or 0)
        print(f"\n  [{sd}] step={det.get('step_pct')}% "
              f"raw={dyn.get('raw_step_pct')}% pts={dyn.get('points_clamp')} "
              f"min_gap={so.get('min_gap_pct')}({so.get('min_gap_src')})")
        print(f"       结构位 given={so.get('given')} in_side={so.get('in_side')} "
              f"dedup={so.get('deduped_out')} 留下={kept} 补点={so.get('filled')} 总={so.get('levels')}")

        A.check(len(legs) == 10, f"{sd} 层数 = 10", f"{len(legs)}")
        A.check(0.66 - 1e-6 <= sides[sd]["step"] <= 2.2 + 1e-6,
                f"{sd} 档距在 0.66~2.2%", f"{sides[sd]['step']}%")
        gaps = [round(legs[i]["px"] - legs[i + 1]["px"], 2) for i in range(len(legs) - 1)]
        bad = [g for g in gaps if not (15 <= abs(g) <= 50)]
        A.check(not bad, f"{sd} 相邻间距全部 15~50 点",
                f"{min(abs(g) for g in gaps)}~{max(abs(g) for g in gaps)}"
                + (f" 违规={bad}" if bad else ""))

    # ── 构成与张数 ──────────────────────────────────────────
    print("\n" + "=" * 78)
    print("③ 构成：7 网格 + 3 马丁（3/4/6 张）")
    print("=" * 78)
    for sd, d in sides.items():
        legs = d["legs"]
        units = [x["unit"] for x in legs]
        lots = [int(x["lots"]) for x in legs]
        n_m = units.count("martin")
        n_g = units.count("grid")
        expect = [3, 1, 1, 1, 4, 1, 1, 1, 1, 6]
        print(f"  [{sd}] units={units}")
        print(f"        lots={lots}  合计={sum(lots)}")
        A.check(n_m == 3, f"{sd} 马丁 3 档", f"{n_m} 档（第 {[i+1 for i,u in enumerate(units) if u=='martin']} 层）")
        A.check(n_g == 7, f"{sd} 网格 7 档", f"{n_g} 档")
        A.check(lots == expect, f"{sd} 张数序列 = {expect}", f"{lots}")
        A.check(sum(lots) == 20, f"{sd} 总张数 = 20", f"{sum(lots)}")

    # ── 马丁深度 ────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("④ 马丁深度（相对现价 −2% / −4% / −6%）")
    print("=" * 78)
    mark = float((sides.get("long") or sides.get("short"))["det"].get("mark") or 0)
    for sd, d in sides.items():
        sgn = 1 if sd == "short" else -1
        mars = [(i + 1, x) for i, x in enumerate(d["legs"]) if x["unit"] == "martin"]
        for need_pct, (idx, x) in zip((2, 4, 6), mars):
            dist = (x["px"] - mark) / mark * 100
            ok = abs(dist) >= need_pct - 1e-6
            A.check(ok, f"{sd} 马{sgn and ''}{idx} 层 深度 ≥ {need_pct}%",
                    f"px={x['px']} 距现价 {dist:+.2f}%")

    # ── 挂单与止损 ──────────────────────────────────────────
    db = os.path.join(ROOT, "data", "accounts", "gate-grid-paper", "account.db")
    con = sqlite3.connect(db)
    orows = list(con.execute("SELECT price, size FROM orders"))
    prows = list(con.execute("SELECT trigger_price, size, rule FROM price_orders"))
    con.close()
    longs = sorted([r for r in orows if r[1] > 0], reverse=True)
    shorts = sorted([r for r in orows if r[1] < 0])
    print("\n" + "=" * 78)
    print("⑤ 实盘挂单与保护")
    print("=" * 78)
    print(f"  多 {len(longs)} 档 {sum(r[1] for r in longs):.0f} 张 | "
          f"空 {len(shorts)} 档 {abs(sum(r[1] for r in shorts)):.0f} 张")
    A.check(len(longs) == 10 and len(shorts) == 10, "两侧各 10 条挂单",
            f"{len(longs)}/{len(shorts)}")
    # 挂单 vs legs 一致性
    for sd, arr in (("long", longs), ("short", shorts)):
        legs_px = sorted([x["px"] for x in (sides.get(sd) or {}).get("legs") or []], reverse=(sd == "long"))
        book_px = sorted([r[0] for r in arr], reverse=(sd == "long"))
        A.check(len(legs_px) == len(book_px) and all(
            abs(p - q) < 0.02 for p, q in zip(legs_px, book_px)),
            f"{sd} 挂单价与 legs 一致",
            f"legs={[round(x,1) for x in legs_px]}" if legs_px != book_px else "")

    A.check(len(prows) == 2, "保护单 = 2 条（每侧 1 条共用 SL）", f"{len(prows)} 条")
    for trig, size, rule in prows:
        side = "long" if rule == 2 else "short"
        sgn = 1 if side == "long" else -1
        d = sides.get(side) or {}
        deep = (min(x["px"] for x in d.get("legs") or [0]) if side == "long"
                else max(x["px"] for x in d.get("legs") or [0]))
        dist_pts = abs(trig - deep)
        side_ok = (trig < mark) if side == "long" else (trig > mark)
        A.check(side_ok, f"{side} SL 在正确一侧", f"SL={trig} mark={mark}")
        A.check(280 <= dist_pts <= 520, f"{side} SL 距最深档 300 点量级",
                f"{dist_pts:.1f} 点（{trig} vs 最深档 {deep}）")
        A.check(abs(size) >= 19.5, f"{side} SL 覆盖全梯张数", f"{abs(size):.0f} 张")

    # ── 止盈表 ──────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("⑥ 逐档止盈（档位表）")
    print("=" * 78)
    lv_path = os.path.join(ROOT, "data", "bots", a.bot, "state", "ladder_levels.json")
    if not os.path.exists(lv_path):
        A.add(FAIL, "档位表存在", "文件缺失 → ensure_grid_tp 会 no_ladder_table")
        table = []
    else:
        table = json.load(open(lv_path, encoding="utf-8")).get("levels") or []
    A.check(len(table) == 20, "档位表 20 条", f"{len(table)} 条")
    no_tp = [x for x in table if not x.get("tp_target")]
    A.check(not no_tp, "每档都有 tp_target", f"{len(no_tp)} 条缺失")
    bad_dir = [x for x in table
               if x.get("tp_target") and ((x["side"] == "long" and x["tp_target"] <= x["price"])
                                          or (x["side"] == "short" and x["tp_target"] >= x["price"]))]
    A.check(not bad_dir, "TP 方向正确（多在上/空在下）", f"{len(bad_dir)} 条反了")
    for unit in ("grid", "martin"):
        us = [x for x in table if x["unit"] == unit]
        if us:
            dists = sorted({round(abs(x["tp_target"] - x["price"]) / x["price"] * 100, 3) for x in us})
            print(f"  {unit:6s} {len(us)} 档  止盈距离 {dists}%")
    base_n = len([x for x in table if x["unit"] == "base"])
    if base_n == 0:
        A.add(WARN, "底仓档位", "0 档（配置 base_position.enabled=false）")

    # ── 风险 ────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("⑦ 敞口与风险（按 50x / quanto 0.01 粗算）")
    print("=" * 78)
    total_lots = sum(r[1] for r in longs) + abs(sum(r[1] for r in shorts))
    notional = sum(r[0] * abs(r[1]) for r in orows) * 0.01
    margin = notional / 50
    print(f"  总张数 {total_lots:.0f} | 名义 {notional:.1f} U | 50x 保证金 {margin:.1f} U")
    equity = 67.0
    A.add(WARN if margin / equity > 0.8 else PASS, "保证金占权益 ≤ 80%",
          f"{margin:.1f}U / {equity:.0f}U = {margin / equity * 100:.0f}%")

    print("\n" + "=" * 78)
    print("审计结论")
    print("=" * 78)
    nf, nw = A.report()
    return 0 if nf == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
