# -*- coding: utf-8 -*-
"""在指定版本目录下跑同一场景，输出 JSON（用于线上版 vs 本地版对照）。

用法：python _ver_probe.py <repo_root> <scenario>
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1])
SCEN = sys.argv[2]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

MARK = 2332.71
EMA200 = 2454.0

TPL = {
    "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
    "martin_slots": {3: 6, 7: 7, 10: 9}, "martin": [], "snap_tol_pct": 0.15,
    "anchor_mode": "trailing", "always_on": True, "symbol": "ETH_USDT", "sl_pct": 5.0,
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
    def __init__(self, pos=0.0, side="long"):
        self.mark = MARK
        self.pos, self.side = pos, side
        self.book, self.prot = [], []

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
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        self.prot = [p for p in self.prot if p["id"] != pid]
        return {}


def db(close4h, bbmid=None):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
    mid = bbmid if bbmid is not None else MARK
    half = MARK * 2.6 / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793272500, 1, MARK, MARK + 5, MARK - 5, MARK,
                 7.5, mid, mid + half, mid - half, None, mid * .999, mid * .998))
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "4h", 1793246400, 1, close4h, close4h, close4h, close4h,
                 None, None, None, None, EMA200, None, None))
    con.commit()
    con.close()
    return f.name


def run(direction_mode, entry_action, pos=0.0, side="long", close4h=None, bbmid=None,
        state=None):
    d = db(close4h if close4h is not None else MARK, bbmid)
    c = Client(pos=pos, side=side)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
    t = dict(TPL)
    t["dynamic"] = dict(TPL["dynamic"], db_path=d)
    t["direction_mode"] = direction_mode
    # 新版才认识这个键；旧版忽略
    t["direction_hysteresis"] = {"enabled": True, "interval": "4h",
                                 "long_keep_ratio": 0.98, "short_keep_ratio": 1.005,
                                 "state_file": str(state or (Path(tempfile.mkdtemp()) / "d.json"))}
    ex.ladder_template = t
    ex._round_meta = {}
    slug = entry_action
    sl = MARK * 0.92 if "long" in slug else MARK * 1.08
    leg = Intent(action=entry_action, symbol="ETH_USDT", price=MARK * 0.995,
                 size_usd=MARK * 0.01, sl=sl)
    rep = type("R", (), {"results": []})()
    ex._complete_ladder([leg], rep)
    Path(d).unlink(missing_ok=True)
    dets = [r.detail for r in rep.results
            if getattr(r, "action", "") == "ladder_template" and r.detail]
    det = next((x for x in dets if "step_pct" in x or x.get("blocked")), {})
    sizes = [float(b["size"]) for b in c.book]
    prices = [float(b["price"]) for b in c.book]
    return {
        "blocked": det.get("blocked"),
        "entries": len(c.book),
        "entry_side": ("short" if sizes and max(sizes) < 0 else
                       "long" if sizes else "none"),
        "entry_range": ([round(min(prices), 1), round(max(prices), 1)] if prices else None),
        "protectors": [
            {"rule": int((p.get("trigger") or {}).get("rule") or 0),
             "price": round(float((p.get("trigger") or {}).get("price") or 0), 1),
             "size": round(float((p.get("initial") or {}).get("size") or 0), 1)}
            for p in c.prot
        ],
    }


out = {}
if SCEN == "flip_signal":
    # 持多 3 张，模型给 open_short（翻向）；signal 模式 —— 线上当前就是这个模式
    out = run("signal", "open_short", pos=3.0, side="long")
elif SCEN == "flip_auto":
    # 持空 3 张，4H 涨破判多（auto）
    out = run("auto", "hold", pos=3.0, side="short", close4h=EMA200 * 1.01)
elif SCEN == "same_side":
    # 持空 3 张 + 判空（同向，正常铺）
    out = run("auto", "hold", pos=3.0, side="short", close4h=EMA200 * 0.978)
elif SCEN == "chop":
    # 抖动：15m BB 中轨在现价上下摆动（旧版裸判每轮翻），4H 收盘全在迟滞带内（新版不翻）
    # 关键：**必须共用同一个 direction state**，否则新版每轮都当"首次"→ 也翻
    st = Path(tempfile.mkdtemp()) / "d.json"
    seq = []
    for d in (0.0012, -0.0018, 0.0025, -0.0022, 0.0008):
        bb = MARK * (1 - d)
        c4 = EMA200 * (1 + d * 0.5)
        seq.append(run("auto", "hold", pos=0.0, close4h=c4, bbmid=bb,
                       state=st)["entry_side"])
    out = {"sides": seq, "flips": sum(1 for a, b in zip(seq, seq[1:]) if a != b)}

print(json.dumps(out, ensure_ascii=False))
