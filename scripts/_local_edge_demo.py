#!/usr/bin/env python3
"""演示边缘模式：区间 2300~2380 下，价格在下半 → 下沿接多；在上半 → 上沿接空。"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

ATR, LO, HI, BARS = 7.5, 2300.0, 2380.0, 96


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark):
        self.mark = mark
        self.book, self.prot = [], []

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
        return self.prot

    def place_order(self, body):
        self.book.append(dict(body, id=f"o{len(self.book)}"))
        return {"id": f"o{len(self.book)}"}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        return {}


class Report:
    def __init__(self):
        self.results = []


def db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema20 REAL, ema50 REAL)")
    rows = []
    for i in range(BARS):
        h = HI if i == 5 else 2350.0
        low = LO if i == 40 else 2330.0
        rows.append(("ETH_USDT", "15m", 1793000000 + i * 900, 1, 2340.0, h, low,
                     2340.0, ATR, 2340.0, 2350.0, 2330.0, 2340.0, 2340.0))
    con.executemany("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return f.name


TPL = {
    "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
    "martin_slots": {3: 6, 7: 7, 10: 9}, "martin": [], "snap_tol_pct": 0.15,
    "direction_mode": "auto",
    "edge": {"enabled": True, "lookback": 96, "levels": 5, "step_atr": 0.5,
             "sl_atr": 2.0, "martin_slots": {1: 9, 3: 7, 5: 6}},
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
}


def run(tag, mark):
    c = Client(mark)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"])
    ex.ladder_template = dict(TPL, dynamic=dict(TPL["dynamic"], db_path=db()))
    ex._round_meta = {}
    rep = Report()
    ex._complete_ladder([Intent(action="open_long", symbol="ETH_USDT",
                                price=mark * 0.995, size_usd=mark * 0.01,
                                sl=mark * 0.9)], rep)
    dets = [s.detail for s in rep.results
            if getattr(s, "action", "") == "ladder_template" and s.detail]
    det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
    Path(ex.ladder_template["dynamic"]["db_path"]).unlink(missing_ok=True)
    dyn = det.get("dynamic") or {}
    edge = dyn.get("edge") or {}
    rows = sorted(((float(b["price"]), int(float(b["size"]))) for b in c.book),
                  reverse=True)
    print(f"\n=== {tag} ===")
    print(f"  区间 [{edge.get('lo')}, {edge.get('hi')}]（{edge.get('bars')} 根，"
          f"跨度 {edge.get('range_pct')}%）  mark={mark}")
    print(f"  方向={dyn.get('side')}  半区={edge.get('half')}  档数={len(rows)}")
    print("  挂单: " + "  ".join(f"{p:.1f}×{s:+d}" for p, s in rows))
    if rows:
        lo_p, hi_p = min(p for p, _ in rows), max(p for p, _ in rows)
        print(f"  覆盖 {lo_p:.1f} ~ {hi_p:.1f}，"
              f"距区间中点 {(lo_p + hi_p) / 2:.1f} 与现价 {mark}")
    for p in c.prot[-1:]:
        print(f"  SL: {p['trigger']['price']} rule={p['trigger']['rule']} "
              f"size={p['initial']['size']}")


run("① 价在下半区（2320）→ 下沿接多", 2320.0)
run("② 价在上半区（2360）→ 上沿接空", 2360.0)
run("③ 价在中轨附近（2340）→ 上半，接空", 2340.0)
