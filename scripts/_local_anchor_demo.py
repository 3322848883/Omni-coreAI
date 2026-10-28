#!/usr/bin/env python3
"""本地演示 P0 固定锚点：4 轮（价格不动 / 小幅波动 / 越过重锚阈值 / 回撤）。

对照 trailing（现状）：每轮都重铺 10 层。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

STATE = Path(tempfile.mkdtemp()) / "ladder_anchor.json"
TPL_BASE = {
    "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
    "martin": [{"pct": 4.2, "lots": 6}, {"pct": 5.4, "lots": 7},
               {"pct": 6.6, "lots": 9}],
}


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark):
        self.mark = mark
        self.placed, self.cancelled = [], []
        self.placed_total = 0

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-mtg"}
                for b in self.placed]

    def list_price_orders(self, sym):
        return []

    def place_order(self, body):
        rec = dict(body)
        rec["id"] = f"e{self.placed_total}"
        self.placed_total += 1
        self.placed.append(rec)
        return {"id": f"e{len(self.placed)}"}

    def place_price_order(self, body):
        return {"id": "p1"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.placed = [b for b in self.placed if b.get("id") != oid]
        return {}

    def cancel_price_order(self, pid):
        return {}


class Report:
    def __init__(self):
        self.results = []


def db_for(mark):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    half = mark * 0.013
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, mark, mark + 5, mark - 5, mark,
                 mark * 0.0033, mark, mark + half, mark - half))
    con.commit()
    con.close()
    return f.name


def run(mode, rounds):
    print(f"\n{'=' * 62}\n模式 = {mode}\n{'=' * 62}")
    client = Client(rounds[0][1])
    ex = Executor(client, symbols_whitelist=["ETH_USDT"])
    tpl = dict(TPL_BASE)
    tpl["dynamic"] = dict(TPL_BASE["dynamic"])
    if mode == "fixed":
        tpl.update({"anchor_mode": "fixed", "reanchor_threshold_pct": 2.0,
                    "anchor_state_file": str(STATE)})
    total = 0
    for tag, mark in rounds:
        if mode == "trailing" and client.placed:
            # 真实流程里 trailing 每轮 replace 会撤掉旧梯（stale cleanup）
            client.placed.clear()
        client.mark = mark
        db = db_for(mark)
        ex.ladder_template = dict(tpl)
        ex.ladder_template["dynamic"] = dict(tpl["dynamic"], db_path=db)
        rep = Report()
        leg = Intent(action="open_long", symbol="ETH_USDT", price=mark * 0.995,
                     size_usd=mark * 0.01, sl=mark * 0.92)
        before = client.placed_total
        ex._complete_ladder([leg], rep)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
        ai = det.get("anchor_info") or {}
        added = client.placed_total - before
        total += added
        print(f"  {tag:<22} mark={mark:>7.2f} anchor={det.get('anchor')} "
              f"drift={ai.get('drift_pct')} reanchored={ai.get('reanchored')} "
              f"新增={added} 盘面={len(client.placed)} 层")
        Path(db).unlink(missing_ok=True)
    print(f"  → 4 轮合计新挂 {total} 次（trailing 应为 40）")
    return total


ROUNDS = [
    ("① 首轮建锚", 2340.0),
    ("② 价格不动", 2340.5),
    ("③ +1.9%（未越阈值）", 2384.2),
    ("④ +2.1%（越阈值重锚）", 2389.1),
]
t_fixed = run("fixed", ROUNDS)
t_trail = run("trailing", ROUNDS)
print(f"\n结论：fixed={t_fixed} 次 vs trailing={t_trail} 次，"
      f"API 写减少 {100 * (1 - t_fixed / max(t_trail, 1)):.0f}%")
