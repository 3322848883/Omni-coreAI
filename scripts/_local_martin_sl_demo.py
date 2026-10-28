#!/usr/bin/env python3
"""本地验证三项修复（用线上真实参数）：
① 马丁 ATR 化（ATR 7.5 → 深度 0.96/1.92/3.20%）
② 档位吸附结构位（给了 structure_levels 时贴过去）
③ SL 护栏（模型给 2160 → 拉回 [最深-3ATR, 最深-1ATR]）
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

MARK = 2332.71
ATR = 7.5
CFG = json.loads((ROOT / "config/bots/martin-grid.yaml").read_text(encoding="utf-8")
                 .split("ladder_template:")[1].split("\n  ")[0]) if False else None


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self):
        self.placed = []

    def get_last_price(self, sym):
        return MARK

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return []

    def list_price_orders(self, sym):
        return []

    def place_order(self, body):
        self.placed.append(body)
        return {"id": f"o{len(self.placed)}"}

    def place_price_order(self, body):
        return {"id": "p1"}

    def cancel_order(self, oid):
        return {}

    def cancel_price_order(self, pid):
        return {}


class Report:
    def __init__(self):
        self.results = []


def db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 ATR, MARK, MARK * 1.013, MARK * 0.987))
    con.commit()
    con.close()
    return f.name


TPL = {
    "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
    "snap_tol_pct": 0.15,
    "martin_slots": {2: 6, 6: 7, 10: 9},
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
    "martin": [],
}


def run(tag, sl=2160.0, levels=None):
    c = Client()
    ex = Executor(c, symbols_whitelist=["ETH_USDT"])
    ex.ladder_template = dict(TPL, dynamic=dict(TPL["dynamic"], db_path=db()))
    ex._round_meta = {"structure_levels": levels} if levels else {}
    rep = Report()
    leg = Intent(action="open_long", symbol="ETH_USDT", price=MARK * 0.995,
                 size_usd=MARK * 0.01, sl=sl)
    ex._complete_ladder([leg], rep)
    dets = [s.detail for s in rep.results
            if getattr(s, "action", "") == "ladder_template" and s.detail]
    det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
    dyn = det.get("dynamic") or {}
    prices = sorted((float(o["price"]) for o in c.placed), reverse=True)
    print(f"\n=== {tag} ===")
    print(f"  step={det.get('step_pct')}%  ATR={dyn.get('atr')} ({dyn.get('atr_pct')}%)")
    print(f"  模型={dyn.get('model')} slots={dyn.get('slots')}")
    print(f"  SL 调整 = {dyn.get('sl_adjusted')}")
    if levels:
        print(f"  吸附 = {dyn.get('structure_snap')}")
    print(f"  档位: {[(round(p,2), int(float(o[chr(115)+chr(105)+chr(122)+chr(101)]))) for p, o in zip(prices, sorted(c.placed, key=lambda x: -float(x[chr(112)+chr(114)+chr(105)+chr(99)+chr(101)])))]}")
    if prices:
        deepest = min(prices)
        gaps = [round(prices[i] - prices[i + 1], 2) for i in range(len(prices) - 1)]
        print(f"  最深={deepest:.2f}（-{100 * (MARK - deepest) / MARK:.2f}%）  间距={gaps}")
    return dyn


run("① 马丁 ATR 化 + ③ SL 护栏（模型给 2160）")
run("② 结构吸附：给 5 个结构位", levels=[2330, 2312, 2296, 2280, 2262])

print("\n" + "=" * 62)
print("对比：旧配置（固定 pct 4.2/5.4/6.6）")
TPL_OLD = {k: v for k, v in TPL.items() if k != "martin_slots"}
TPL_OLD.update({"grid_levels": 7, "martin": [{"pct": 4.2, "lots": 6},
                                             {"pct": 5.4, "lots": 7},
                                             {"pct": 6.6, "lots": 9}]})
c = Client()
ex = Executor(c, symbols_whitelist=["ETH_USDT"])
ex.ladder_template = dict(TPL_OLD, dynamic=dict(TPL["dynamic"], db_path=db()))
rep = Report()
ex._complete_ladder([Intent(action="open_long", symbol="ETH_USDT", price=MARK * 0.995,
                            size_usd=MARK * 0.01, sl=2160.0)], rep)
prices = sorted((float(o["price"]) for o in c.placed), reverse=True)
print(f"  最深={min(prices):.2f}（-{100 * (MARK - min(prices)) / MARK:.2f}%）"
      f" → 2 倍于新配置，SL 被迫 ≥ 该深度")
