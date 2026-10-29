# -*- coding: utf-8 -*-
"""本地展示：用当前真实行情跑一次完整铺梯（生产配置），打印档位/张数/SL。"""
import json
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
DATA = json.loads((ROOT / "scripts/_kline_data.json").read_text(encoding="utf-8"))
BARS = DATA["bars"]
H4 = DATA["h4"]


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark):
        self.mark, self.book, self.prot = mark, [], []

    def get_last_price(self, s):
        return self.mark

    def get_contract(self, s):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, s):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, s):
        return self.prot

    def place_order(self, b):
        self.book.append(dict(b, price=float(b["price"]), size=float(b["size"]),
                              id=f"o{len(self.book)}"))
        return {"id": f"o{len(self.book)}"}

    def place_price_order(self, b):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": b["initial"], "trigger": b["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, o):
        self.book = [b for b in self.book if b["id"] != o]
        return {}

    def cancel_price_order(self, o):
        return {}


def swings(i, lookback=96):
    win = BARS[max(0, i - lookback + 1): i + 1]
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


i = len(BARS) - 1
bar = BARS[i]
mark = bar["c"]
win = BARS[max(0, i - 95): i + 1]
lo, hi = min(x["l"] for x in win), max(x["h"] for x in win)
lv = swings(i)
cur4 = H4[-1]

f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
con = sqlite3.connect(f.name)
con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
            "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
            "boll20_upper REAL, boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
half = mark * 2.6 / 200
# **必须插满近 96 根**：`_range_points` 要按 lookback 算区间，只插 1 根会把
# 24h 区间算成单根振幅（实测 20 点）→ 覆盖价距恒取下限 300，测不出真实效果
for b in win:
    is_last = b is bar
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", b["t"], 1, b["o"], b["h"], b["l"], b["c"],
                 8.0 if is_last else None,
                 mark if is_last else None,
                 mark + half if is_last else None,
                 mark - half if is_last else None,
                 None, None, None))
con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ETH_USDT", "4h", cur4["t"], 1, cur4["c"], cur4["c"], cur4["c"], cur4["c"],
             None, None, None, None, cur4["ema200"], None, None))
con.commit()
con.close()

print(f"行情：mark={mark:.2f}  近24h区间 {lo:.1f}~{hi:.1f}（{hi-lo:.0f} 点）")
print(f"4H：收盘 {cur4['c']:.1f} / EMA200 {cur4['ema200']:.1f}"
      f"（{(cur4['c']-cur4['ema200'])/cur4['ema200']*100:+.1f}%）")
print(f"结构位（近 96 根 swing 模拟）：{len(lv)} 个")

for side, act in (("long", "open_long"), ("short", "open_short")):
    c = Client(mark)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
    t = dict(TPL)
    t["dynamic"] = dict(TPL["dynamic"], db_path=f.name)
    t["anchor_state_file"] = str(Path(tempfile.mkdtemp()) / "a.json")
    t["direction_mode"] = "signal"
    ex.ladder_template = t
    ex._round_meta = {"structure_levels": lv}
    rep = type("R", (), {"results": []})()
    sl = mark * (0.92 if side == "long" else 1.08)
    ex._complete_ladder([Intent(action=act, symbol="ETH_USDT", price=mark * 0.99,
                                size_usd=mark * 0.01, sl=sl)], rep)
    dets = [r.detail for r in rep.results
            if getattr(r, "action", "") == "ladder_template" and r.detail]
    det = next((d for d in dets if "step_pct" in d), {})
    dyn = det.get("dynamic") or {}
    so = dyn.get("structure_only") or {}
    sp = dyn.get("sl_points") or {}
    rows = sorted(c.book, key=lambda b: abs(float(b["price"]) - mark))  # 距现价由近到远
    print(f"\n{'='*72}\n{side.upper()}  层数={len(rows)}  "
          f"结构位={so.get('in_side')}/{so.get('given')} 个  补点={so.get('filled')}  "
          f"间距下限={so.get('min_gap_pct')}%")
    print(f"{'档':>3} {'价格':>9} {'距现价':>8} {'张数':>5}")
    for k, b in enumerate(rows, 1):
        px, sz = float(b["price"]), float(b["size"])
        print(f"{k:>3} {px:>9.2f} {(px-mark)/mark*100:>7.2f}% {sz:>5.0f}")
    if c.prot:
        trg = c.prot[-1]["trigger"]
        sl_px = float(trg["price"])
        deepest = (min if side == "long" else max)(float(b["price"]) for b in rows)
        print(f"SL  {sl_px:.2f}  rule={trg['rule']}  size={c.prot[-1]['initial']['size']}")
        print(f"    距现价 {(mark-sl_px)/mark*100:+.2f}%  距最深档 {abs(deepest-sl_px):.0f} 点"
              f"  覆盖价距={sp.get('points')}")

try:
    Path(f.name).unlink(missing_ok=True)
except PermissionError:
    pass
