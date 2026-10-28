#!/usr/bin/env python3
"""举例：新版本（10 档 + 马丁占位 + SL 护栏）走一遍完整行情，与旧版并排比较。

行情：2332.71 → 2325 → 2315 → 2300 → 2293（接近 SL）→ 2320（反弹）
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

MARK = 2332.71
ATR = 7.5
PATH = [("① 建梯", 2332.71), ("② 小跌", 2325.0), ("③ 破位", 2315.0),
        ("④ 深跌", 2300.0), ("⑤ 触底", 2293.0), ("⑥ 反弹", 2320.0)]

NEW = {
    "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
    "martin_slots": {2: 6, 6: 7, 10: 9}, "snap_tol_pct": 0.15, "martin": [],
    "anchor_mode": "trailing",
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
}
OLD = {
    "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
    "martin": [{"pct": 4.2, "lots": 6}, {"pct": 5.4, "lots": 7},
               {"pct": 6.6, "lots": 9}],
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
}


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark):
        self.mark = mark
        self.book = []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-mtg"} for b in self.book]

    def list_price_orders(self, sym):
        return []

    def place_order(self, body):
        rec = dict(body, id=f"o{len(self.book)}")
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        return {"id": "p"}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        return {}


class Report:
    def __init__(self):
        self.results = []


def db(interval_atr=ATR):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    half = MARK * 0.013
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 interval_atr, MARK, MARK + half, MARK - half))
    con.commit()
    con.close()
    return f.name


def walk(tag, tpl):
    print(f"\n{'=' * 78}\n{tag}\n{'=' * 78}")
    c = Client(MARK)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"])
    ex.ladder_template = dict(tpl, dynamic=dict(tpl["dynamic"], db_path=db()))
    rep = Report()
    ex._complete_ladder([Intent(action="open_long", symbol="ETH_USDT",
                                price=MARK * 0.995, size_usd=MARK * 0.01,
                                sl=2160.0)], rep)
    dets = [s.detail for s in rep.results
            if getattr(s, "action", "") == "ladder_template" and s.detail]
    det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
    dyn = det.get("dynamic") or {}
    ladder = sorted(((float(b["price"]), int(float(b["size"]))) for b in c.book),
                    reverse=True)
    sl = (dyn.get("sl_adjusted") or {}).get("adjusted") or 2160.0
    all_entry = sum(sz for _, sz in ladder)
    print(f"  建梯 step={det.get('step_pct')}%  ATR={dyn.get('atr')}  "
          f"最深={ladder[-1][0]:.2f}(-{(MARK - ladder[-1][0]) / MARK * 100:.2f}%)")
    print(f"  档位/张数: " + "  ".join(f"{p:.1f}×{s}" for p, s in ladder))
    print(f"  SL={sl}  覆盖={int(all_entry)} 张（= 入场总量）")

    pos, cost = 0, 0.0
    print(f"\n  {'轮次':<8}{'mark':>9}{'触发成交':>18}{'持仓':>7}{'均价':>9}{'浮亏/盈':>10}")
    for label, mk in PATH:
        hits = [(p, s) for p, s in ladder if mk <= p]
        ladder = [(p, s) for p, s in ladder if mk > p]
        add = sum(s for _, s in hits)
        if add:
            cost = (cost * pos + sum(p * s for p, s in hits)) / (pos + add)
            pos += add
        pnl = (mk - cost) * pos * 0.01 if pos else 0.0
        hit_txt = ", ".join(f"{p:.1f}×{s}" for p, s in hits) or "—"
        print(f"  {label:<8}{mk:>9.2f}{hit_txt:>18}{pos:>7}{cost:>9.2f}{pnl:>+10.2f}")
    Path(ex.ladder_template["dynamic"]["db_path"]).unlink(missing_ok=True)
    return {"pos": pos, "cost": cost, "pnl": (PATH[-1][1] - cost) * pos * 0.01,
            "sl": sl, "deepest": ladder[-1][0] if ladder else None,
            "entry": all_entry}


new = walk("新版本：10 档等距 + 马丁占第 2/6/10 档 + SL 护栏", NEW)
old = walk("旧版本：7 网格 + 马丁固定深度 4.2/5.4/6.6%", OLD)

print(f"\n{'=' * 78}\n对比总结（同一路径，mark 从 2332.71 跌到 2293 再反弹 2320）\n{'=' * 78}")
print(f"{'':<22}{'新版本':>16}{'旧版本':>16}")
print(f"{'入场总量(张)':<22}{new['entry']:>16}{old['entry']:>16}")
print(f"{'最深档距 mark':<22}{(MARK - new['deepest']) / MARK * 100:>15.2f}%"
      f"{(MARK - old['deepest']) / MARK * 100:>15.2f}%")
print(f"{'止损价':<22}{new['sl']:>16.2f}{old['sl']:>16.2f}")
print(f"{'止损距 mark':<22}{(MARK - new['sl']) / MARK * 100:>15.2f}%"
      f"{(MARK - old['sl']) / MARK * 100:>15.2f}%")
print(f"{'成交张数':<22}{new['pos']:>16}{old['pos']:>16}")
print(f"{'均价':<22}{new['cost']:>16.2f}{old['cost']:>16.2f}")
print(f"{'反弹到 2320 盈亏':<22}{new['pnl']:>+16.2f}{old['pnl']:>+16.2f}")
