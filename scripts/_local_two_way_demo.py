# -*- coding: utf-8 -*-
"""双向网格 2.0 · 真实行情多轮演示（带真穿越撮合）。

与旧版（单侧 + auto 切换方向）的区别：这里是**两侧同时挂**，下方多头网格、
上方空头网格各自维护，看它们在真实波动里分别怎么成交、SL 怎么跟。

用法：python scripts/_local_two_way_demo.py [轮数，默认 5]
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
DATA = json.loads((ROOT / "scripts/_kline_data.json").read_text(encoding="utf-8"))
BARS = DATA["bars"]
QUANTO = 0.01
START = 620


class Meta:
    quanto_multiplier = QUANTO
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark):
        self.mark = mark
        self.book, self.prot = [], []
        self.pos_long = 0.0
        self.pos_short = 0.0
        self.avg_long = 0.0
        self.avg_short = 0.0
        self.fills = []
        self.n = 0

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        out = []
        if abs(self.pos_long) > 1e-9:
            out.append({"contract": "ETH_USDT", "side": "long",
                        "size": str(self.pos_long), "entry_price": str(self.avg_long)})
        if abs(self.pos_short) > 1e-9:
            out.append({"contract": "ETH_USDT", "side": "short",
                        "size": str(-self.pos_short),
                        "entry_price": str(self.avg_short)})
        return out

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        rec = dict(body, id=f"o{self.n}", price=float(body["price"]),
                   size=float(body["size"]))
        self.n += 1
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        rec = {"id": f"p{self.n}", "status": "open",
               "initial": body["initial"], "trigger": body["trigger"]}
        self.n += 1
        self.prot.append(rec)
        return {"id": rec["id"]}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, oid):
        self.prot = [p for p in self.prot if p["id"] != oid]
        return {}


def match(c, hi, lo, idx):
    """用本根 K 线撮合：先保护单（止损优先），再入场单。"""
    for p in list(c.prot):
        trg = p.get("trigger") or {}
        try:
            px, rule = float(trg.get("price") or 0), int(trg.get("rule") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        cap = abs(float((p.get("initial") or {}).get("size") or 0))
        if rule == 2 and c.pos_long > 0 and lo <= px:
            q = min(cap, c.pos_long)
            c.fills.append({"i": idx, "side": "close_long", "price": px, "size": q,
                            "pnl": q * (px - c.avg_long) * QUANTO})
            c.pos_long -= q
            if c.pos_long < 1e-9:
                c.pos_long, c.avg_long = 0.0, 0.0
            c.prot.remove(p)
        elif rule == 1 and c.pos_short > 0 and hi >= px:
            q = min(cap, c.pos_short)
            c.fills.append({"i": idx, "side": "close_short", "price": px, "size": q,
                            "pnl": q * (c.avg_short - px) * QUANTO})
            c.pos_short -= q
            if c.pos_short < 1e-9:
                c.pos_short, c.avg_short = 0.0, 0.0
            c.prot.remove(p)
    for b in list(c.book):
        px, sz = float(b["price"]), float(b["size"])
        hit = (sz > 0 and lo <= px) or (sz < 0 and hi >= px)
        if not hit:
            continue
        if sz > 0:
            c.avg_long = ((c.pos_long * c.avg_long + abs(sz) * px) / (c.pos_long + abs(sz))
                          if c.pos_long > 0 else px)
            c.pos_long += abs(sz)
        else:
            c.avg_short = ((c.pos_short * c.avg_short + abs(sz) * px)
                           / (c.pos_short + abs(sz)) if c.pos_short > 0 else px)
            c.pos_short += abs(sz)
        c.fills.append({"i": idx, "side": "open_long" if sz > 0 else "open_short",
                        "price": px, "size": abs(sz), "pnl": 0.0})
        c.book.remove(b)


def main():
    tpl = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
    tpl["two_way"] = True
    tpl["two_way_sides"] = ["long", "short"]
    d = Path(tempfile.mkdtemp())
    dbp = d / "k.db"
    con = sqlite3.connect(dbp)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    base = BARS[START]
    con.execute("INSERT INTO kline VALUES ('ETH_USDT','15m',?,1,?,?,?,?,?,?,?,?)",
                (base["t"], base["o"], base["h"], base["l"], base["c"],
                 7.5, base["c"], base["c"] * 1.013, base["c"] * 0.987))
    con.commit()
    tpl["dynamic"] = dict(tpl.get("dynamic") or {}, db_path=str(dbp))
    tpl["anchor_state_file"] = str(d / "anchor.json")
    tpl["levels_state_file"] = str(d / "levels.json")

    c = Client(float(base["c"]))
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
    ex.ladder_template = tpl
    ex._round_meta = {}

    span = len(BARS) - START - 1
    step = max(1, span // ROUNDS)
    idxs = [START + i * step for i in range(ROUNDS)]
    print(f"双向网格 2.0 · 真实 ETH 15m {ROUNDS} 轮（起始 bar#{START} mark={c.mark:.2f}）")
    print("=" * 100)
    for k, i in enumerate(idxs):
        bar = BARS[i]
        c.mark = float(bar["c"])
        con.execute("UPDATE kline SET t=?, o=?, h=?, l=?, c=?, boll20=?, boll20_upper=?, "
                    "boll20_lower=?",
                    (bar["t"], bar["o"], bar["h"], bar["l"], bar["c"], bar["c"],
                     bar["c"] * 1.013, bar["c"] * 0.987))
        con.commit()
        if k > 0:
            match(c, float(bar["h"]), float(bar["l"]), i)

        class R:
            results = []
        rep = R()
        ex._complete_ladder([], rep)

        longs = sorted(((float(b["price"]), abs(float(b["size"])))
                        for b in c.book if float(b["size"]) > 0), reverse=True)
        shorts = sorted(((float(b["price"]), abs(float(b["size"])))
                         for b in c.book if float(b["size"]) < 0))
        sl2 = [p for p in c.prot if int((p.get("trigger") or {}).get("rule") or 0) == 2]
        sl1 = [p for p in c.prot if int((p.get("trigger") or {}).get("rule") or 0) == 1]

        print(f"\n── 第 {k+1} 轮  bar#{i}  mark={c.mark:.2f}  "
              f"高/低 {float(bar['h']):.2f}/{float(bar['l']):.2f}")
        print(f"   多 {len(longs)} 档 " +
              (f"{longs[0][0]:.2f} → {longs[-1][0]:.2f} 张数 {[int(s) for _, s in longs]}"
               if longs else "（空）"))
        print(f"   空 {len(shorts)} 档 " +
              (f"{shorts[0][0]:.2f} → {shorts[-1][0]:.2f} 张数 {[int(s) for _, s in shorts]}"
               if shorts else "（空）"))
        if sl2 and longs:
            p = float(sl2[0]["trigger"]["price"])
            print(f"   多SL rule2 {p:.2f} size={(sl2[0]['initial'] or {}).get('size')}"
                  f"  距最深档 {longs[-1][0] - p:.0f}")
        if sl1 and shorts:
            p = float(sl1[0]["trigger"]["price"])
            print(f"   空SL rule1 {p:.2f} size={(sl1[0]['initial'] or {}).get('size')}"
                  f"  距最高档 {p - shorts[-1][0]:.0f}")
        print(f"   持仓 多 {c.pos_long:.2f} @ {c.avg_long:.2f} | "
              f"空 {c.pos_short:.2f} @ {c.avg_short:.2f}")
        if k > 0:
            newf = [f for f in c.fills if f["i"] == i]
            if newf:
                for f in newf:
                    print(f"   成交 {f['side']:<11} {f['size']:>4.1f} @ {f['price']:.2f}"
                          + (f"  pnl={f['pnl']:+.3f}" if f["pnl"] else ""))
            else:
                print("   成交 —（本根未穿档）")

    con.close()
    pnl = sum(f["pnl"] for f in c.fills)
    opens = sum(1 for f in c.fills if f["pnl"] == 0)
    print("\n" + "=" * 100)
    print(f"总计：成交 {len(c.fills)} 笔（开 {opens} / 平 {len(c.fills) - opens}）  "
          f"已实现 PnL {pnl:+.3f} U")
    unreal = (c.pos_long * (c.mark - c.avg_long) * QUANTO
              - c.pos_short * (c.avg_short - c.mark) * QUANTO)
    print(f"      末态 多 {c.pos_long:.2f} 张 / 空 {c.pos_short:.2f} 张  浮动 {unreal:+.3f} U")


if __name__ == "__main__":
    main()
