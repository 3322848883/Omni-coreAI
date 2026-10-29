# -*- coding: utf-8 -*-
"""本地演示：方向迟滞带 vs 裸判（同一段抖动行情）。

数据源：服务器 2026-10-29 实测 4H EMA200 = 2454.0（ETH_USDT）。本地 kline.db 是
10-26 的旧 schema（无 ema200 列），所以这里用实测值 + 构造价格序列。

跑：python scripts/_local_hysteresis_demo.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor, _hysteresis_side  # noqa: E402

EMA200 = 2454.0
ATR = 7.5

TPL = {
    "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
    "martin_slots": {3: 6, 7: 7, 10: 9}, "martin": [], "snap_tol_pct": 0.15,
    "anchor_mode": "trailing", "always_on": True, "symbol": "ETH_USDT", "sl_pct": 5.0,
    "direction_mode": "auto",
    "direction_hysteresis": {"enabled": True, "interval": "4h",
                             "long_keep_ratio": 0.98, "short_keep_ratio": 1.005},
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
}


class Client:
    def __init__(self, mark):
        self.mark = mark
        self.book = []
        self.prot = []
        self.placed_total = 0

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        class M:
            quanto_multiplier = 0.01
            size_min = 0.1
            size_step = 0.1
            order_price_round = 0.05
        return M()

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-mtg"}
                for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def get_positions(self):
        return []

    def place_order(self, body):
        rec = dict(body, id=f"e{self.placed_total}")
        self.placed_total += 1
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b.get("id") != oid]
        return {}

    def cancel_price_order(self, oid):
        self.prot = [p for p in self.prot if p.get("id") != oid]
        return {}


def make_db(close4h, mark):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "4h", 1793246400, 1, close4h, close4h, close4h,
                 close4h, None, None, None, None, EMA200))
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793272500, 1, mark, mark, mark, mark,
                 ATR, mark, mark * 1.013, mark * 0.987, None))
    con.commit()
    con.close()
    return f.name


def naive_side(close):
    return "long" if close >= EMA200 else "short"


def run_path(title, path):
    print(f"\n{'='*78}\n{title}\n{'='*78}")
    print(f"{'轮':>3} {'4H收盘':>9} {'偏离EMA':>9} {'裸判':>6} {'迟滞':>6} "
          f"{'引擎side':>8}  {'铺单方向':>10}  理由")
    prev = None
    naive_flips = hyst_flips = 0
    last_naive = last_hyst = None

    st = Path(tempfile.mkdtemp()) / "dirstate.json"
    for i, close in enumerate(path, 1):
        # 纯函数对照
        h_side, reason = _hysteresis_side(close, EMA200, prev)
        n_side = naive_side(close)
        if last_naive and n_side != last_naive:
            naive_flips += 1
        if last_hyst and h_side != last_hyst:
            hyst_flips += 1
        last_naive, last_hyst = n_side, h_side
        prev = h_side

        # 引擎链路（真跑 _complete_ladder）
        db = make_db(close, close)
        c = Client(close)
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        tpl = dict(TPL)
        tpl["dynamic"] = dict(TPL["dynamic"], db_path=db)
        tpl["direction_hysteresis"] = dict(TPL["direction_hysteresis"], state_file=str(st))
        ex.ladder_template = tpl
        ex._round_meta = {}
        rep = type("R", (), {"results": []})()

        from omnialpha.executor import Intent
        ex._complete_ladder([Intent(action="hold", symbol="ETH_USDT")], rep)
        dets = [r.detail for r in rep.results
                if getattr(r, "action", "") == "ladder_template" and r.detail]
        det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
        eng_side = (det.get("dynamic") or {}).get("side", "?")
        sizes = [float(b["size"]) for b in c.book]
        direction = "空(上方)" if (sizes and max(sizes) < 0) else (
            "多(下方)" if sizes else "无")
        dev = (close - EMA200) / EMA200 * 100
        print(f"{i:>3} {close:>9.2f} {dev:>8.2f}% {n_side:>6} {h_side:>6} "
              f"{eng_side:>8}  {direction:>10}  {reason}")
        Path(db).unlink(missing_ok=True)

    print(f"\n翻向次数：裸判 {naive_flips} 次   →   迟滞 {hyst_flips} 次")
    return naive_flips, hyst_flips


# ① 抖动：围绕 EMA200 ±0.3%，10 轮原地打转
chop = [EMA200 * (1 + d) for d in
        (0.0012, -0.0018, 0.0025, -0.0022, 0.0008, -0.0015, 0.0021, -0.0009, 0.0016, -0.0011)]
a = run_path("① 抖动行情：价格贴着 EMA200 上下 ±0.3%（真实震荡最常见形态）", chop)

# ② 真跌破：从下方一路跌到 -3%
down = [EMA200 * (1 + d) for d in (-0.001, -0.005, -0.012, -0.020, -0.030)]
b = run_path("② 单边下跌：收盘跌到 EMA200 −3%（真突破，应当转空）", down)

# ③ 真涨破：空头状态下涨到 +1.5%
up = [EMA200 * (1 + d) for d in (-0.02, -0.01, -0.002, 0.002, 0.008, 0.015)]
c = run_path("③ 由空转多：从 −2% 涨到 +1.5%（越过 1.005 带才翻多）", up)

print(f"\n{'='*78}\n结论\n{'='*78}")
print(f"抖动行情： 裸判翻 {a[0]} 次（每轮都在多空之间跳）  vs  迟滞翻 {a[1]} 次")
print(f"真突破：   下跌方向迟滞翻 {b[1]} 次、上涨迟滞翻 {c[1]} 次 —— 该翻的时候照翻")
print("含义：迟滞带只过滤掉「噪声翻向」，不阻止真趋势切换。")
