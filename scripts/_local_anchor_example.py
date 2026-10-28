#!/usr/bin/env python3
"""举例：同一段价格路径下，trailing（线上）vs fixed（本地）的挂单与成交差异。

路径取自典型形态：小跌 → 深跌到 2326 → 反弹回 2338。
模拟规则：买入限价单 price >= 当轮 mark 即视为成交（穿价）。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

STATE = Path(tempfile.mkdtemp()) / "anchor.json"
BASE = {
    "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
    "martin": [{"pct": 4.2, "lots": 6}, {"pct": 5.4, "lots": 7},
               {"pct": 6.6, "lots": 9}],
}
PATH = [("① 开盘", 2340.0), ("② 小跌", 2336.5), ("③ 破位", 2326.0),
        ("④ 再跌", 2312.0), ("⑤ 深跌", 2298.0), ("⑥ 反弹", 2318.0),
        ("⑦ 回到原位", 2338.0)]


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self):
        self.mark = 0.0
        self.book = []
        self.n = 0
        self.placed_cnt = 0
        self.cancel_cnt = 0
        self.fills = []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-mtg"}
                for b in self.book]

    def list_price_orders(self, sym):
        return []

    def place_order(self, body):
        rec = dict(body, id=f"o{self.n}")
        self.n += 1
        self.placed_cnt += 1
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        return {"id": "p"}

    def cancel_order(self, oid):
        self.cancel_cnt += 1
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        return {}

    def settle(self):
        """穿价成交：买单 price >= mark 即成交。"""
        hits = [b for b in self.book if float(b["price"]) >= self.mark]
        self.book = [b for b in self.book if float(b["price"]) < self.mark]
        self.fills.extend((self.mark, float(b["price"]), int(float(b["size"])))
                          for b in hits)


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


def run(mode):
    c = Client()
    ex = Executor(c, symbols_whitelist=["ETH_USDT"])
    tpl = dict(BASE)
    tpl["dynamic"] = dict(BASE["dynamic"])
    if mode == "fixed":
        tpl.update({"anchor_mode": "fixed", "reanchor_threshold_pct": 2.0,
                    "anchor_state_file": str(STATE)})
    rows = []
    for tag, mark in PATH:
        before_p, before_c = c.placed_cnt, c.cancel_cnt
        if mode == "trailing" and c.book:
            # 真实流程：replace 撤掉旧梯
            for b in list(c.book):
                c.cancel_order(b["id"])
        c.mark = mark
        db = db_for(mark)
        ex.ladder_template = dict(tpl)
        ex.ladder_template["dynamic"] = dict(tpl["dynamic"], db_path=db)
        n_fill_before = len(c.fills)
        c.settle()                       # 先按当轮价格吃单
        rep = Report()
        leg = Intent(action="open_long", symbol="ETH_USDT", price=mark * 0.995,
                     size_usd=mark * 0.01, sl=mark * 0.92)
        ex._complete_ladder([leg], rep)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
        rows.append({
            "tag": tag, "mark": mark,
            "anchor": det.get("anchor"),
            "placed": c.placed_cnt - before_p, "cancelled": c.cancel_cnt - before_c,
            "fills": len(c.fills) - n_fill_before,
            "book": len(c.book),
        })
        Path(db).unlink(missing_ok=True)
    return rows, c


for mode, label in (("trailing", "线上实盘（trailing：每轮跟最新价）"),
                    ("fixed", "本地新版（fixed：锚点固定，2% 才重锚）")):
    rows, c = run(mode)
    print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")
    print(f"{'轮次':<8}{'mark':>8}{'锚点':>9}{'挂单':>6}{'撤单':>6}{'成交':>6}{'盘面':>6}")
    for r in rows:
        print(f"{r['tag']:<8}{r['mark']:>8.1f}{r['anchor'] or 0:>9.1f}"
              f"{r['placed']:>6}{r['cancelled']:>6}{r['fills']:>6}{r['book']:>6}")
    tot_p = sum(r["placed"] for r in rows)
    tot_c = sum(r["cancelled"] for r in rows)
    tot_f = sum(r["fills"] for r in rows)
    vol = sum(sz for _, _, sz in c.fills)
    print(f"{'合计':<8}{'':>8}{'':>9}{tot_p:>6}{tot_c:>6}{tot_f:>6}{'':>6}")
    if c.fills:
        avg = sum(px * sz for _, px, sz in c.fills) / vol
        print(f"  成交明细：" + ", ".join(f"{px:.2f}×{sz}" for _, px, sz in c.fills))
        print(f"  累计买入 {vol} 张，均价 {avg:.2f}")
