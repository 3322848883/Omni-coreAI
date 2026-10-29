# -*- coding: utf-8 -*-
"""探针：多空翻向时，已存在的**反向持仓**如何处理。

风险假设：side 翻成 long 后，保护单按 long 语义挂（rule=2 跌破卖出、挂在下方），
但持仓实际是 short → 空头既没有有效止损，又可能被 reverse 单误平。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

TPL = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
MARK = 2332.71
EMA200 = 2454.0


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK, pos=0.0, side="long"):
        self.mark, self.pos, self.side = mark, pos, side
        self.book, self.prot, self.cancelled = [], [], []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if not self.pos:
            return []
        signed = abs(self.pos) if self.side == "long" else -abs(self.pos)
        return [{"contract": "ETH_USDT", "side": self.side,
                 "size": str(signed), "entry_price": str(self.mark)}]

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        rec = dict(body, id=f"o{len(self.book)}")
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        self.prot = [p for p in self.prot if p["id"] != pid]
        return {}


def db(close4h, atr=7.5, bw=2.6):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
    half = MARK * bw / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793272500, 1, MARK, MARK + 5, MARK - 5, MARK,
                 atr, MARK, MARK + half, MARK - half, None, MARK * .999, MARK * .998))
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "4h", 1793246400, 1, close4h, close4h, close4h, close4h,
                 None, None, None, None, EMA200, None, None))
    con.commit()
    con.close()
    return f.name


def probe(title, pos_side, close4h):
    print(f"\n{'='*70}\n{title}\n{'='*70}")
    sd = tempfile.mkdtemp()
    d = db(close4h)
    c = Client(pos=3.0, side=pos_side)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
    t = dict(TPL)
    t["dynamic"] = dict(TPL.get("dynamic") or {}, db_path=d)
    t["anchor_state_file"] = str(Path(sd) / "a.json")
    t["direction_hysteresis"] = dict(t.get("direction_hysteresis") or {},
                                     state_file=str(Path(sd) / "d.json"))
    t["direction_mode"] = "auto"
    ex.ladder_template = t
    ex._round_meta = {}
    rep = type("R", (), {"results": []})()
    ex._complete_ladder([Intent(action="hold", symbol="ETH_USDT")], rep)
    dets = [r.detail for r in rep.results
            if getattr(r, "action", "") == "ladder_template" and r.detail]
    det = next((x for x in dets if "step_pct" in x), dets[0] if dets else {})
    side = (det.get("dynamic") or {}).get("side")
    print(f"  持仓: {pos_side} 3 张   4H收盘: {close4h:.2f}   引擎 side: {side}")
    print(f"  入场单 {len(c.book)} 条:",
          [(round(float(b['price']), 1), int(float(b['size']))) for b in c.book][:3])
    for p in c.prot:
        trg = p.get("trigger") or {}
        print(f"  保护单 rule={trg.get('rule')} 触发={trg.get('price')} "
              f"size={p.get('initial', {}).get('size')}")
    Path(d).unlink(missing_ok=True)


probe("① 持空 3 张 + 4H 涨破(判多) → 翻向，空头保护怎么办？", "short", EMA200 * 1.01)
probe("② 持多 3 张 + 4H 跌破(判空) → 反向翻向镜像", "long", EMA200 * 0.978)
probe("③ 持空 3 张 + 4H 仍判空 → 同向，正常", "short", EMA200 * 0.978)
