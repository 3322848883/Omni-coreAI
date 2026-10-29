# -*- coding: utf-8 -*-
"""反向持仓护栏：翻向时不许按新方向铺梯 + 挂错方向的 SL。

实测 BUG（2026-10-29）：持空 3 张时 4H 涨破判多 → 引擎铺 10 条多头入场单，
并把 SL 按 long 语义挂成 rule=2（跌破卖出、位于下方）。结果空头仓位**完全没有
止损**（真止损在上方），跌到下方反而被"提前止盈"平掉。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

TPL = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
MARK = 2332.71
EMA200 = 2454.0


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, pos=0.0, side="long"):
        self.mark, self.pos, self.side = MARK, pos, side
        self.book, self.prot, self.cancelled = [], [], []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if not self.pos:
            return []
        signed = abs(self.pos) if self.side == "long" else -abs(self.pos)
        return [{"contract": "ETH_USDT", "side": self.side,
                 "size": str(signed), "entry_price": str(self.mark)}]

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        rec = dict(body, id=f"o{len(self.book)}")
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        self.prot = [p for p in self.prot if p["id"] != pid]
        return {}


def _db(close4h):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
    half = MARK * 2.6 / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793272500, 1, MARK, MARK + 5, MARK - 5, MARK,
                 7.5, MARK, MARK + half, MARK - half, None, MARK * .999, MARK * .998))
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "4h", 1793246400, 1, close4h, close4h, close4h, close4h,
                 None, None, None, None, EMA200, None, None))
    con.commit()
    con.close()
    return f.name


def _run(close4h, pos=0.0, side="long", book=None):
    sd = Path(tempfile.mkdtemp())
    db = _db(close4h)
    c = Client(pos=pos, side=side)
    for b in (book or []):
        c.book.append(b)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
    t = dict(TPL)
    t["dynamic"] = dict(TPL.get("dynamic") or {}, db_path=db)
    t["anchor_state_file"] = str(sd / "a.json")
    t["direction_hysteresis"] = dict(t.get("direction_hysteresis") or {},
                                     state_file=str(sd / "d.json"))
    t["direction_mode"] = "auto"
    ex.ladder_template = t
    ex._round_meta = {}

    class Rep:
        results = []

    rep = Rep()
    ex._complete_ladder([Intent(action="hold", symbol="ETH_USDT")], rep)
    Path(db).unlink(missing_ok=True)
    dets = [r.detail for r in rep.results
            if getattr(r, "action", "") == "ladder_template" and r.detail]
    det = next((d for d in dets if d.get("blocked") or "step_pct" in d), {})
    return c, det


class TestReversePositionGuard(unittest.TestCase):
    def test_short_position_vs_long_side_is_blocked(self):
        """持空 + 4H 涨破判多 → 拦截，不铺多头入场单。"""
        c, det = _run(EMA200 * 1.01, pos=3.0, side="short")
        self.assertEqual(det.get("blocked"), "reverse_position")
        self.assertEqual(c.book, [], "翻向时不得按新方向铺梯")
        self.assertEqual(det["position"], {"side": "short", "size": 3.0})

    def test_rescue_sl_covers_short_position_on_upper_side(self):
        """兜底 SL 必须是空头的真止损侧：上方 + rule=1 + size 为正。"""
        c, det = _run(EMA200 * 1.01, pos=3.0, side="short")
        sl = [p for p in c.prot if (p.get("trigger") or {}).get("rule") == 1]
        self.assertTrue(sl, "空头必须有 rule=1 的上方保护")
        self.assertGreater(float(sl[-1]["trigger"]["price"]), c.mark)
        self.assertGreater(float(sl[-1]["initial"]["size"]), 0)

    def test_long_position_vs_short_side_is_blocked(self):
        """镜像：持多 + 4H 跌破判空 → 拦截 + 下方 rule=2。"""
        c, det = _run(EMA200 * 0.978, pos=3.0, side="long")
        self.assertEqual(det.get("blocked"), "reverse_position")
        self.assertEqual(c.book, [])
        sl = [p for p in c.prot if (p.get("trigger") or {}).get("rule") == 2]
        self.assertTrue(sl)
        self.assertLess(float(sl[-1]["trigger"]["price"]), c.mark)

    def test_same_side_not_blocked(self):
        """同向（持空 + 判空）必须正常铺满 10 层。"""
        c, det = _run(EMA200 * 0.978, pos=3.0, side="short")
        self.assertIsNone(det.get("blocked"))
        self.assertEqual(len(c.book), 10)

    def test_no_position_not_blocked(self):
        c, det = _run(EMA200 * 1.01, pos=0.0)
        self.assertIsNone(det.get("blocked"))
        self.assertEqual(len(c.book), 10)

    def test_rescue_covers_position_plus_same_side_orders(self):
        """只覆盖持仓不够：旧同向梯成交后会重新裸仓 → 必须 持仓+同向挂单。"""
        book = [{"id": f"x{i}", "price": 2400 + i * 5, "size": -1} for i in range(5)]
        _c, det = _run(EMA200 * 1.01, pos=3.0, side="short", book=book)
        prot = det.get("protection") or {}
        self.assertAlmostEqual(float(prot.get("size") or 0), 8.0, places=2)
        self.assertAlmostEqual(float(prot.get("position") or 0), 3.0, places=2)

    def test_int64_truncated_short_is_detected(self):
        """size=0（int64 截断）+ mode=dual_short，靠 value 反算 0.1 张也必须识别为空头。"""
        sd = Path(tempfile.mkdtemp())
        db = _db(EMA200 * 1.01)
        c = Client(pos=0.0)
        c.get_positions = lambda: [{"contract": "ETH_USDT", "size": "0",
                                    "mode": "dual_short", "value": "-2.33",
                                    "mark_price": "2333", "entry_price": "2333"}]
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
        t = dict(TPL)
        t["dynamic"] = dict(TPL.get("dynamic") or {}, db_path=db)
        t["anchor_state_file"] = str(sd / "a.json")
        t["direction_hysteresis"] = dict(t.get("direction_hysteresis") or {},
                                         state_file=str(sd / "d.json"))
        t["direction_mode"] = "auto"
        ex.ladder_template = t
        ex._round_meta = {}

        class Rep:
            results = []

        rep = Rep()
        ex._complete_ladder([Intent(action="hold", symbol="ETH_USDT")], rep)
        Path(db).unlink(missing_ok=True)
        dets = [r.detail for r in rep.results
                if getattr(r, "action", "") == "ladder_template" and r.detail]
        det = next((d for d in dets if d.get("blocked") or "step_pct" in d), {})
        self.assertEqual(det.get("blocked"), "reverse_position")
        self.assertEqual(det["position"]["side"], "short")
        self.assertAlmostEqual(det["position"]["size"], 0.1, places=2)


if __name__ == "__main__":
    unittest.main()
