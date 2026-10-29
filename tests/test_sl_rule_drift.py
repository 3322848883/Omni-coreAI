# -*- coding: utf-8 -*-
"""SL 规则漂移：张数够但价格不符合当前规则 → 必须按新规则重挂。

实测事故（2026-10-29 部署 sl_distance 后）：旧 SL 仍挂在 2197（−3×ATR 时代的价），
因为覆盖校验只比张数（`covered(29>=29)`）→ 新规则的 300~500 点永远不生效。
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
MARK = 2327.0
OLD = [2311.21, 2300.77, 2290.32, 2279.87, 2269.42,
       2258.98, 2248.53, 2238.08, 2227.63, 2217.19]


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, sl_price="2197.38", sl_size=-29, old=None):
        self.mark = MARK
        self.book = [{"id": f"old{i}", "price": str(p), "size": 1}
                     for i, p in enumerate(old if old is not None else OLD)]
        self.prot = ([{"id": "p0", "status": "open",
                       "initial": {"size": sl_size},
                       "trigger": {"price": sl_price, "rule": 2}}]
                     if sl_price is not None else [])
        self.cancelled = []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": b["price"], "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        self.book.append(dict(body, id=f"n{len(self.book)}"))
        return {"id": "x"}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, oid):
        self.prot = [p for p in self.prot if p["id"] != oid]
        return {}


def _run(sl_price="2197.38", sl_size=-29, lo=None, hi=None,
         state=None, client=None):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1, 1, MARK, hi or MARK + 50, lo or MARK - 50, MARK,
                 8.0, MARK, MARK * 1.013, MARK * 0.987, None, MARK * .999, MARK * .998))
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "4h", 1, 1, MARK, MARK, MARK, MARK,
                 None, None, None, None, 2452.7, None, None))
    con.commit()
    con.close()

    c = client or Client(sl_price, sl_size)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
    t = dict(TPL)
    t["dynamic"] = dict(TPL.get("dynamic") or {}, db_path=f.name)
    t["anchor_state_file"] = str(state or (Path(tempfile.mkdtemp()) / "a.json"))
    t["direction_mode"] = "signal"
    ex.ladder_template = t
    ex._round_meta = {}

    class Rep:
        results = []

    rep = Rep()
    ex._complete_ladder([Intent(action="hold", symbol="ETH_USDT")], rep)
    dets = [r.detail for r in rep.results
            if getattr(r, "action", "") == "ladder_template" and r.detail]
    det = next((d for d in dets if "step_pct" in d or d.get("protection")
                or d.get("sl_rewrite") or d.get("skipped")), {})
    return c, det


class TestSlRuleDrift(unittest.TestCase):
    def test_stale_sl_is_replaced_by_new_rule(self):
        """旧 SL 2197（−3×ATR 时代）→ 按 300~500 点规则重挂到 1800 附近。"""
        c, det = _run()
        prot = det.get("protection") or {}
        self.assertIn("rule_drift", str(prot.get("reason")),
                      f"应识别为规则漂移，实际 {prot}")
        self.assertEqual(len(c.prot), 1, "旧的必须撤掉，只留新的")
        new_px = float(c.prot[0]["trigger"]["price"])
        self.assertLess(new_px, 2197.0, "新 SL 应更远（300~500 点覆盖价距）")
        pts = (det.get("dynamic") or {}).get("sl_points") or {}
        self.assertGreaterEqual(pts.get("points") or 0, 300.0)
        self.assertLessEqual(pts.get("points") or 0, 500.0)

    def test_sl_unchanged_when_price_already_matches(self):
        """SL 价格已符合当前规则 → 不动（避免每轮重复撤挂）。"""
        _c, det = _run()
        pts = (det.get("dynamic") or {}).get("sl_points") or {}
        target = float(pts.get("deepest") or 0) - float(pts.get("points") or 0)
        c2, det2 = _run(sl_price=f"{target:.2f}")
        prot = det2.get("protection") or {}
        self.assertIn("covered", str(prot.get("skipped") or prot), f"不该重挂：{prot}")
        self.assertEqual(len(c2.prot), 1)

    def test_already_full_still_rewrites_stale_sl(self):
        """阶梯满（already_full）时也必须维护 SL —— 否则改配置后旧 SL 永远换不掉。

        实测事故：部署 sl_distance 后连续 6 轮都是 already_full 直接 return，
        SL 停在 2197（−3×ATR 时代）不动。
        """
        st = Path(tempfile.mkdtemp()) / "a.json"
        c, det1 = _run(state=st)                       # 第一轮建锚
        self.assertNotEqual(det1.get("skipped"), "already_full")
        # 把 SL 篡改成旧规则价（模拟"部署前留下的老单"）
        # ⚠️ 张数必须用**档位张数总和**，不是 `len(book)` —— 马丁档是 6/7/9 张，
        # 10 条挂单共 29 张；用条数会让 need 算错、走成 cover_gap 而非 rule_drift。
        n = int(sum(abs(float(b.get("size") or 0)) for b in c.book))
        self.assertGreater(n, len(c.book), "本节依赖马丁档张数 > 1")
        c.prot = [{"id": "stale", "status": "open", "initial": {"size": -n},
                   "trigger": {"price": "2197.38", "rule": 2}}]
        _c2, det2 = _run(state=st, client=c)           # 第二轮：锚点未漂移
        self.assertEqual(det2.get("skipped"), "already_full", f"{det2}")
        rw = det2.get("sl_rewrite") or {}
        self.assertIn("rule_drift", str(rw.get("reason")), f"应重挂 SL：{rw}")
        self.assertEqual(len(c.prot), 1)
        self.assertLess(float(c.prot[0]["trigger"]["price"]), 2197.0)

    def test_cover_gap_still_wins_when_size_short(self):
        """张数不足（-3）时仍走 cover_gap，不受漂移逻辑影响。"""
        c, det = _run(sl_price="2197.38", sl_size=-3)
        prot = det.get("protection") or {}
        self.assertIn("cover_gap", str(prot.get("reason")), f"{prot}")
        self.assertEqual(len(c.prot), 1)


if __name__ == "__main__":
    unittest.main()
