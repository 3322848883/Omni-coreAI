#!/usr/bin/env python3
"""第 2 批本地测试（3 组）：幂等重跑 / AI+高波动组合 / 贴近现价。

① 幂等：同一盘面连跑两次 → 第二次不得再挂（already_full），也不能撤已有档
② AI 建议 + 高波动：ai_step=1.0 但跨度只允许 0.857 → 必须被 clamp 并留痕，
   马丁压缩、10 层、SL 覆盖全部仍成立
③ 贴近现价：已有档位就在 mark 附近（0.1%）→ 不得重复挂同价
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

MARK = 2340.0
POS = 3.0


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, sl_size=-3):
        self.placed, self.placed_prot, self.cancelled_prot = [], [], []
        self.cancelled_orders = []
        self.price_orders = ([{"id": "old", "status": "open",
                               "trigger": {"price": "2160", "rule": 2},
                               "initial": {"size": sl_size}}] if sl_size else [])

    def get_last_price(self, sym):
        return MARK

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return [{"contract": "ETH_USDT", "side": "long", "size": str(POS),
                 "entry_price": str(MARK)}]

    def list_orders(self, sym):
        return [{"id": f"e{i}", "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-mtg"}
                for i, b in enumerate(self.placed)]

    def list_price_orders(self, sym):
        return self.price_orders

    def place_order(self, body):
        self.placed.append(body)
        return {"id": f"e{len(self.placed)}"}

    def place_price_order(self, body):
        self.placed_prot.append(body)
        self.price_orders.append({"id": f"p{len(self.placed_prot)}", "status": "open",
                                  "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.placed_prot)}"}

    def cancel_price_order(self, pid):
        self.cancelled_prot.append(pid)
        return {}

    def cancel_order(self, oid):
        self.cancelled_orders.append(oid)
        return {}


class Report:
    def __init__(self):
        self.results = []


def make_db(atr, bw_pct):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    half = MARK * bw_pct / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 atr, MARK, MARK + half, MARK - half))
    con.commit()
    con.close()
    return f.name


def build(atr=7.8, bw=2.6, round_meta=None):
    db = make_db(atr, bw)
    c = Client()
    ex = Executor(c, symbols_whitelist=["ETH_USDT"])
    ex.ladder_template = {
        "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
        "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                    "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                    "scale_martin": True, "max_martin_pct": 9.0, "db_path": db},
        "martin": [{"pct": 4.2, "lots": 6}, {"pct": 5.4, "lots": 7},
                   {"pct": 6.6, "lots": 9}],
    }
    ex._round_meta = dict(round_meta or {})
    leg = Intent(action="open_long", symbol="ETH_USDT", price=MARK * 0.995,
                 size_usd=MARK * 0.01, sl=MARK * 0.92)
    return ex, c, leg, db


def call(ex, c, leg):
    rep = Report()
    ex._complete_ladder([leg], rep)
    det = next((s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail), {})
    return det


def check(name, cond):
    print(f"    {'PASS' if cond else 'FAIL'}  {name}")
    return bool(cond)


print("=" * 64)
print("① 幂等：同一盘面连跑两次")
print("=" * 64)
ex, c, leg, db = build()
d1 = call(ex, c, leg)
n1 = len(c.placed)
d2 = call(ex, c, leg)
n2 = len(c.placed)
print(f"  第一次挂 {n1} 条，第二次新增 {n2 - n1} 条；"
      f"第二次 detail={ {k: d2.get(k) for k in ('skipped',)} }")
r1 = [
    check("第一次铺满 10 层", n1 == 10),
    check("第二次不重复挂", n2 - n1 == 0),
    check("第二次标记 already_full", d2.get("skipped") == "already_full"),
    check("第二次未撤任何档", c.cancelled_orders == []),
]
Path(db).unlink(missing_ok=True)

print("\n" + "=" * 64)
print("② AI 建议 1.0% + 高波动（跨度只允许 0.857%）")
print("=" * 64)
ex, c, leg, db = build(atr=30.0, bw=6.0, round_meta={"ladder_step_pct": 1.0})
d = call(ex, c, leg)
dyn = d.get("dynamic") or {}
prices = sorted((float(b["price"]) for b in c.placed), reverse=True)
deepest = (MARK - prices[-1]) / MARK * 100 if prices else 0
print(f"  step={d.get('step_pct')}%  ai={dyn.get('ai_step_pct')}  "
      f"clamped={dyn.get('ai_step_clamped')}  有效上限={dyn.get('effective_max_step_pct')}")
print(f"  层数={len(prices)}  最深={deepest:.2f}%  SL={c.placed_prot[0]['initial']['size'] if c.placed_prot else None}")
r2 = [
    check("AI 建议被 clamp 到 0.857", abs((d.get("step_pct") or 0) - 6 / 7) < 1e-3),
    check("留痕 ai_step_clamped", dyn.get("ai_step_clamped") is True),
    check("仍铺满 10 层", len(prices) == 10),
    check("马丁最深 ≤9%", deepest <= 9.05),
    check("SL 覆盖 32 张", (c.placed_prot[0]["initial"]["size"] if c.placed_prot else 0) == -32),
]
Path(db).unlink(missing_ok=True)

print("\n" + "=" * 64)
print("③ 已有档位贴在现价附近（mark-0.1%）")
print("=" * 64)
ex, c, leg, db = build()
c.placed.append({"contract": "ETH_USDT", "size": 1,
                 "price": str(round(MARK * 0.999, 2)), "tif": "gtc", "text": "t-mtg"})
d = call(ex, c, leg)
prices = [float(b["price"]) for b in c.placed]
dups = [p for p in set(round(x, 2) for x in prices)
        if sum(1 for y in prices if abs(y - p) / p <= 0.003) > 1]
print(f"  总挂单 {len(prices)} 条，同价组 {len(dups)} 个，撤单 {c.cancelled_orders}")
r3 = [
    check("无同价重复档", not dups),
    check("贴近现价的档被撤（不在模板价上）", len(c.cancelled_orders) >= 1),
    check("最终层数 = 10", len(prices) == 10),
]
Path(db).unlink(missing_ok=True)

allr = r1 + r2 + r3
print("\n" + "=" * 64)
print(f"总结果：{sum(allr)}/{len(allr)} 项通过")
sys.exit(0 if all(allr) else 1)
