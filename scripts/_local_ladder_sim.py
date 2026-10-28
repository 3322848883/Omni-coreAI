#!/usr/bin/env python3
"""本地验证：用真实指标样本跑两次 _complete_ladder（普通 / 高波动）。

不连交易所、不改线上：假 client 只记录下单，DB 用本地临时 sqlite。
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

SAMPLE = json.loads(Path(__file__).with_name("_sample.json").read_text(encoding="utf-8"))
TPL = {
    "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": 1.2, "max_span_pct": 6.0,
                "max_martin_pct": 9.0,
                "scale_martin": True},
    "martin": [{"pct": 4.2, "lots": 6}, {"pct": 5.4, "lots": 7}, {"pct": 6.6, "lots": 9}],
}


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class FakeClient:
    def __init__(self, mark):
        self.mark = mark
        self.placed = []
        self.price_orders = []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def list_orders(self, sym):
        return []

    def list_price_orders(self, sym):
        return []

    def get_positions(self):
        return []

    def cancel_order(self, oid):
        return {}

    def place_order(self, body):
        self.placed.append(body)
        return {"id": f"o{len(self.placed)}"}

    def place_price_order(self, body):
        self.price_orders.append(body)
        return {"id": "p1"}


class Report:
    def __init__(self):
        self.results = []


def make_db(row):
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


def run(tag, row):
    db = make_db(row)
    mark = float(row[4])
    c = FakeClient(mark)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=TPL)
    rep = Report()
    leg = Intent(action="open_long", symbol="ETH_USDT", price=mark * 0.995,
                 size_usd=mark * 0.01, sl=mark * 0.92)
    ex._dynamic_step = ex._dynamic_step  # noqa: B018  (保留可读性)
    # 指向本地样本库
    tpl = dict(TPL)
    tpl["dynamic"] = dict(TPL["dynamic"], db_path=db)
    ex.ladder_template = tpl
    ex._complete_ladder([leg], rep)

    det = next((s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail), {})
    dyn = det.get("dynamic") or {}
    print(f"\n=== {tag} ===")
    print(f"  bar t={row[0]}  close={mark:.2f}  atr14={row[5]:.2f}  "
          f"boll20={row[6]:.2f}")
    print(f"  atr%={dyn.get('atr_pct')}  带宽%={dyn.get('bw_pct')}  "
          f"raw={dyn.get('raw_step_pct')}  → step={det.get('step_pct')}%")
    prices = [(float(o["price"]), float(o["size"])) for o in c.placed]
    prices.sort(reverse=True)
    print(f"  铺出 {len(prices)} 层（张数 {[int(s) for _, s in prices]}）：")
    for px, sz in prices:
        print(f"    {px:>9.2f}  ×{int(sz)}")
    gaps = [round(prices[i][0] - prices[i + 1][0], 2) for i in range(len(prices) - 1)]
    print(f"  相邻间距（点）：{gaps}")
    print(f"  总跨度：{(prices[0][0] - prices[-1][0]):.2f} 点 "
          f"({(prices[0][0] - prices[-1][0]) / mark * 100:.2f}%)")
    print(f"  保护单：{[p['trigger']['price'] + '/' + str(p['initial']['size']) for p in c.price_orders]}")
    Path(db).unlink(missing_ok=True)
    return prices


last = SAMPLE["recent"][0]          # 最新收盘 bar
hi = SAMPLE["high_vol"]             # 20 天内带宽最大的一根
run("第 1 次：当前波动", last)
run("第 2 次：历史高波动", hi)
