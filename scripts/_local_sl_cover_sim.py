#!/usr/bin/env python3
"""本地验证 SL 覆盖校验：三种场景（无保护 / 不足 / 已足够）。

设计口径（`_sl_cover_cap` 文档）：SL 应覆盖 = 已成交持仓 + 同向未成交入场单。
实测线上曾出现 持仓 3 + 挂单 29 → SL 仍 -3（29 张裸仓），本次修下限兜底。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

SAMPLE = json.loads(Path(__file__).with_name("_sample.json").read_text(encoding="utf-8"))
BAR = SAMPLE["recent"][0]
MARK = float(BAR[4])


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, pos=0.0, price_orders=None):
        self.mark = MARK
        self.pos = pos
        self.price_orders = list(price_orders or [])
        self.placed = []
        self.placed_prot = []
        self.cancelled_prot = []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if self.pos == 0:
            return []
        side = "long" if self.pos > 0 else "short"
        return [{"contract": "ETH_USDT", "side": side, "size": str(abs(self.pos)),
                 "entry_price": str(self.mark)}]

    def list_orders(self, sym):
        return [{"id": f"e{i}", "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open",
                 "text": b.get("text", "t-mtg")}
                for i, b in enumerate(self.placed)]

    def list_price_orders(self, sym):
        return self.price_orders

    def place_order(self, body):
        self.placed.append(body)
        return {"id": f"e{len(self.placed)}"}

    def place_price_order(self, body):
        self.placed_prot.append(body)
        self.price_orders.append({
            "id": f"p{len(self.placed_prot)}", "status": "open",
            "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.placed_prot)}"}

    def cancel_price_order(self, pid):
        self.cancelled_prot.append(pid)
        return {}

    def cancel_order(self, oid):
        return {}


class Report:
    def __init__(self):
        self.results = []


def make_db(row):
    import sqlite3
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", int(row[0]), 1, *row[1:9]))
    con.commit()
    con.close()
    return f.name


def run(tag, pos, preset):
    db = make_db(BAR)
    c = Client(pos=pos, price_orders=preset)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"])
    ex.ladder_template = {
        "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
        "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                    "min_step_pct": 0.45, "max_step_pct": 1.2, "max_span_pct": 6.0,
                    "scale_martin": True, "max_martin_pct": 9.0, "db_path": db},
        "martin": [{"pct": 4.2, "lots": 6}, {"pct": 5.4, "lots": 7},
                   {"pct": 6.6, "lots": 9}],
    }
    rep = Report()
    leg = Intent(action="open_long", symbol="ETH_USDT", price=MARK * 0.995,
                 size_usd=MARK * 0.01, sl=MARK * 0.92)
    ex._complete_ladder([leg], rep)
    det = next((s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail), {})
    entry_total = sum(float(b["size"]) for b in c.placed)
    need = pos + entry_total
    print(f"\n=== {tag} ===")
    print(f"  持仓 {pos:g} + 入场 {entry_total:g} 张 → 应覆盖 {need:g}")
    print(f"  模板 protection 步骤: {det.get('protection')}")
    print(f"  新挂保护单: {[(p['trigger']['price'], p['initial']['size']) for p in c.placed_prot]}")
    print(f"  撤销的旧保护单: {c.cancelled_prot}")
    Path(db).unlink(missing_ok=True)


# ① 完全没有保护单
run("场景 1：无保护单", pos=0.0, preset=[])
# ② 已有 SL 但只覆盖持仓（线上实测形态：持仓 3 / 挂单 29 / SL -3）
run("场景 2：SL 覆盖不足", pos=3.0, preset=[
    {"id": "old", "status": "open", "trigger": {"price": "2160", "rule": 2},
     "initial": {"size": -3}}])
# ③ 已有 SL 覆盖足够（应原样不动）
run("场景 3：SL 已足够", pos=3.0, preset=[
    {"id": "old", "status": "open", "trigger": {"price": "2160", "rule": 2},
     "initial": {"size": -32}}])
