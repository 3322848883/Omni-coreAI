#!/usr/bin/env python3
"""本地 5 连测：5 档波动（含数据缺失回退）× 校验 10 层/间距/SL 覆盖/马丁上限。

每档独立跑 `_complete_ladder`（假 client，不碰交易所），最后打印 PASS/FAIL 表。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

MARK = 2335.0
POS = 3.0


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, sl_size=-3):
        self.placed, self.placed_prot, self.cancelled_prot = [], [], []
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
        return {}


class Report:
    def __init__(self):
        self.results = []


def make_db(atr, up, lo):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 atr, MARK if atr is not None else None, up, lo))
    con.commit()
    con.close()
    return f.name


def run(tag, atr, up, lo):
    db = make_db(atr, up, lo)
    c = Client()
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
    Path(db).unlink(missing_ok=True)

    prices = sorted((float(b["price"]) for b in c.placed), reverse=True)
    sizes = [int(float(b["size"])) for b in sorted(c.placed,
                                                  key=lambda x: -float(x["price"]))]
    entry = sum(sizes)
    step = det.get("step_pct")
    dyn = det.get("dynamic") or {}
    prot = (det.get("protection") or {})
    sl_size = c.placed_prot[0]["initial"]["size"] if c.placed_prot else None
    deepest = (MARK - prices[-1]) / MARK * 100 if prices else 0

    checks = {
        "10 层": len(prices) == 10,
        "张数 1×7+6/7/9": sizes[:7] == [1] * 7 and sizes[7:] == [6, 7, 9],
        "间距单调且≥0.3%": all(
            (prices[i] - prices[i + 1]) / MARK * 100 >= 0.3 for i in range(len(prices) - 1)),
        "SL 覆盖 = 持仓+入场": sl_size == -(POS + entry) if sl_size else True,
        "马丁最深 ≤9%": deepest <= 9.05,
        "step ∈[0.45,1.2] 或回退 0.5": (step is None) or (0.45 - 1e-9 <= step <= 1.2 + 1e-9)
        or abs(step - 0.5) < 1e-9,
    }
    print(f"\n=== {tag} ===")
    print(f"  atr={atr}  带宽={(up - lo) / MARK * 100 if atr else None:.2f}%"
          if atr else f"  atr=None（数据缺失）")
    print(f"  step={step}%  raw={dyn.get('raw_step_pct')}  "
          f"martin_ratio={dyn.get('martin_scaled_by')}  "
          f"compressed={dyn.get('martin_compressed')}")
    print(f"  层数={len(prices)}  张数={sizes}  最深={deepest:.2f}%  SL={sl_size}")
    if prices:
        print(f"  最高 {prices[0]:.2f} → 最低 {prices[-1]:.2f}")
    for k, ok in checks.items():
        print(f"    {'PASS' if ok else 'FAIL'}  {k}")
    return all(checks.values())


CASES = [
    ("① 极低波动（ATR 2，触发 min 托底）", 2.0, MARK * 1.002, MARK * 0.998),
    ("② 当前波动（ATR 7.8）", 7.8, MARK * 1.013, MARK * 0.987),
    ("③ 中等放大（ATR 15）", 15.0, MARK * 1.02, MARK * 0.98),
    ("④ 极高波动（ATR 40，跨度封顶+马丁压缩）", 40.0, MARK * 1.06, MARK * 0.94),
    ("⑤ 指标缺失（atr/boll 全 NULL，回退固定档距）", None, None, None),
]

results = [run(*c) for c in CASES]
print("\n" + "=" * 50)
print(f"总结果：{sum(results)}/{len(results)} 档全部通过")
sys.exit(0 if all(results) else 1)
