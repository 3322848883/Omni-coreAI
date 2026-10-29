#!/usr/bin/env python3
"""演示双向阶梯：多头（下方）/ 空头（上方）/ auto 随趋势翻转。"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

PROD = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
MARK = 2332.71
ATR = 7.5


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK, pos=None):
        self.mark, self.pos = mark, pos
        self.book, self.prot = [], []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if not self.pos:
            return []
        side, size = self.pos
        return [{"contract": "ETH_USDT", "side": side, "size": str(size),
                 "entry_price": str(self.mark)}]

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


def db(mark, bb=None, e20=None, e50=None):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema20 REAL, ema50 REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, mark, mark + 5, mark - 5, mark,
                 ATR, bb if bb is not None else mark, mark * 1.013, mark * 0.987,
                 e20, e50))
    con.commit()
    con.close()
    return f.name


def run(tag, action, mark=MARK, pos=None, auto=False, bb=None, e20=None, e50=None,
        client=None):
    c = client or Client(mark=mark, pos=pos)
    c.mark = mark
    ex = Executor(c, symbols_whitelist=["ETH_USDT"])
    tpl = dict(PROD)
    tpl["dynamic"] = dict(PROD.get("dynamic") or {}, db_path=db(mark, bb, e20, e50))
    tpl["anchor_mode"] = "trailing"
    if auto:
        tpl["direction_mode"] = "auto"
    ex.ladder_template = tpl
    ex._round_meta = {}
    rep = Report()
    sl = mark * 0.92 if action.endswith("long") else mark * 1.08
    ex._complete_ladder([Intent(action=action, symbol="ETH_USDT", price=mark * 0.995,
                                size_usd=mark * 0.01, sl=sl)], rep)
    dets = [s.detail for s in rep.results
            if getattr(s, "action", "") == "ladder_template" and s.detail]
    det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
    Path(tpl["dynamic"]["db_path"]).unlink(missing_ok=True)
    dyn = det.get("dynamic") or {}
    rows = sorted(((float(b["price"]), int(float(b["size"]))) for b in c.book),
                  reverse=True)
    print(f"\n=== {tag} ===")
    print(f"  side={dyn.get('side')}  step={det.get('step_pct')}%  "
          f"auto={dyn.get('auto_direction')}")
    if rows:
        print("  档位/张数: " + "  ".join(f"{p:.1f}×{s:+d}" for p, s in rows))
        side_ok = all(p < mark for p, _ in rows) if dyn.get("side") == "long" \
            else all(p > mark for p, _ in rows)
        print(f"  位置检查：{'全部在现价正确一侧 ✓' if side_ok else '❌ 有档位在错误一侧'}")
    for p in c.prot[-1:]:
        print(f"  SL: trigger={p['trigger']['price']} rule={p['trigger']['rule']} "
              f"size={p['initial']['size']}")
    return c, det


run("① 多头（模型 open_long）", "open_long", pos=("long", 3.0))
run("② 空头（模型 open_short）", "open_short", pos=("short", 3.0))
run("③ auto：价在上轨侧 + EMA 多头排列 → 做多", "open_long", auto=True,
    bb=MARK * 0.995, e20=MARK * 1.002, e50=MARK)
run("④ auto：价跌破 BB 中轨 → 转做空", "open_long", auto=True,
    bb=MARK * 1.02, e20=MARK * 0.98, e50=MARK)
