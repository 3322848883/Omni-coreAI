# -*- coding: utf-8 -*-
"""共享 SL 覆盖下限兜底（2026-10-28 实测 BUG 回归）。

口径（`_sl_cover_cap` 文档）：SL 应覆盖 = 已成交持仓 + 同向未成交入场单。
线上实测：持仓 3 + 挂单 29 → SL 仍 -3（29 张裸仓）→ 模板必须补下限。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

MARK = 2360.0


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, pos=0.0, price_orders=None):
        self.pos = pos
        self.price_orders = list(price_orders or [])
        self.placed = []
        self.placed_prot = []
        self.cancelled_prot = []

    def get_last_price(self, sym):
        return MARK

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if not self.pos:
            return []
        return [{"contract": "ETH_USDT", "side": "long",
                 "size": str(abs(self.pos)), "entry_price": str(MARK)}]

    def list_orders(self, sym):
        return [{"id": f"e{i}", "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": b.get("text", "t-mtg")}
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


def _db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 8.0, MARK, MARK * 1.01, MARK * 0.99))
    con.commit()
    con.close()
    return f.name


class TestSlCoverage(unittest.TestCase):
    def _run(self, pos, preset):
        db = _db()
        c = Client(pos=pos, price_orders=preset)
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        ex.ladder_template = {
            "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5,
                        "bb_div": 6.0, "min_step_pct": 0.45, "max_step_pct": 1.2,
                        "max_span_pct": 6.0, "scale_martin": True,
                        "max_martin_pct": 9.0, "db_path": db},
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
        return c, det

    def test_no_protection_places_full_cover(self):
        c, det = self._run(0.0, [])
        entry = sum(float(b["size"]) for b in c.placed)
        self.assertEqual(len(c.placed_prot), 1)
        self.assertEqual(c.placed_prot[0]["initial"]["size"], -int(entry))
        self.assertIn("cover_gap", (det.get("protection") or {}).get("reason", ""))

    def test_under_cover_replaced_then_old_cancelled(self):
        preset = [{"id": "old", "status": "open",
                   "trigger": {"price": "2160", "rule": 2},
                   "initial": {"size": -3}}]
        c, det = self._run(3.0, preset)
        entry = sum(float(b["size"]) for b in c.placed)
        need = int(3 + entry)
        self.assertEqual(c.placed_prot[0]["initial"]["size"], -need,
                         "SL 必须覆盖 持仓 + 全部入场")
        self.assertEqual(c.cancelled_prot, ["old"], "旧的小尺寸 SL 必须撤掉")

    def test_enough_cover_untouched(self):
        """覆盖**精确等于**敞口时不动（防止每轮重挂 SL 抖动）。"""
        c0, _ = self._run(3.0, [])          # 先量出 need
        need = c0.placed_prot[0]["initial"]["size"]
        preset = [{"id": "old", "status": "open",
                   "trigger": {"price": "2160", "rule": 2},
                   "initial": {"size": need}}]
        c, det = self._run(3.0, preset)
        self.assertEqual(c.placed_prot, [], "覆盖已足够时不得重复挂")
        self.assertEqual(c.cancelled_prot, [], "也不得撤")
        self.assertTrue((det.get("protection") or {}).get("skipped"))

    def test_over_cover_is_tightened(self):
        """覆盖**超过**敞口也要收紧（2026-10-30 实盘：换档后 37 张压 29 张敞口）。

        挂一张平不完的 reduce_only 单语义不干净（交易所只按实际持仓平），
        还会让「这是不是最新规则」的判断失真。
        """
        preset = [{"id": "old", "status": "open",
                   "trigger": {"price": "2160", "rule": 2},
                   "initial": {"size": -99}}]
        c, det = self._run(3.0, preset)
        entry = sum(float(b["size"]) for b in c.placed)
        need = int(3 + entry)
        self.assertEqual(len(c.placed_prot), 1, "过度覆盖必须重挂")
        self.assertEqual(c.placed_prot[0]["initial"]["size"], -need,
                         "必须收紧到实际敞口")
        self.assertEqual(c.cancelled_prot, ["old"])
        self.assertIn("cover_over", (det.get("protection") or {}).get("reason", ""),
                      f"应留 cover_over 痕迹：{det.get('protection')}")


if __name__ == "__main__":
    unittest.main()
