# -*- coding: utf-8 -*-
"""探针：结构位档位生成（档位数 / 补点 / 张数落位）。"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402

TPL = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
DATA = json.loads((ROOT / "scripts/_kline_data.json").read_text(encoding="utf-8"))
BARS = DATA["bars"]


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class C:
    def __init__(self, mark):
        self.mark, self.book, self.prot = mark, [], []

    def get_last_price(self, s):
        return self.mark

    def get_contract(self, s):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, s):
        return self.book

    def list_price_orders(self, s):
        return self.prot

    def place_order(self, b):
        self.book.append(dict(b, id=f"o{len(self.book)}"))
        return {"id": "x"}

    def place_price_order(self, b):
        self.prot.append(b)
        return {"id": "p"}

    def cancel_order(self, o):
        return {}

    def cancel_price_order(self, o):
        return {}


def swings(bars, i, lookback=96):
    win = bars[max(0, i - lookback + 1): i + 1]
    lv = []
    for k in range(2, len(win) - 2):
        h, l = win[k]["h"], win[k]["l"]
        if h >= max(x["h"] for x in win[k-2:k]) and h >= max(x["h"] for x in win[k+1:k+3]):
            lv.append(h)
        if l <= min(x["l"] for x in win[k-2:k]) and l <= min(x["l"] for x in win[k+1:k+3]):
            lv.append(l)
    if win:
        lv += [min(x["l"] for x in win), max(x["h"] for x in win)]
    return sorted({round(v, 2) for v in lv})


for i in (450, 500, 600, 700, 799):
    bar = BARS[i]
    mark = bar["c"]
    win = BARS[max(0, i - 95): i + 1]
    lo, hi = min(x["l"] for x in win), max(x["h"] for x in win)
    lv = swings(BARS, i, 96)
    c = C(mark)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
    ex._round_meta = {"structure_levels": lv}
    ln = {"range_points": hi - lo, "range": {"lo": lo, "hi": hi}}
    for side in ("long", "short"):
        sw, note = ex._structure_targets("ETH_USDT", mark, side,
                                         TPL["levels_dynamic"], ln, TPL)
        print(f"i={i} mark={mark:.1f} 区间{hi-lo:.0f}点 结构位{len(lv)}个 "
              f"side={side:5s} → {len(sw):2d} 档 {note}")
        if sw:
            print("     档位:", [round(p, 1) for p, _ in sw])
            print("     张数:", [int(l) for _, l in sw])
