# -*- coding: utf-8 -*-
"""阶梯跟随性回归（2026-10-29 真实 K 线回放抓到的 5 个 bug）。

每个用例对应一条实测症状：
  ① SL 钉子：`max(cur, target)` 遇上「盘上已有 SL」→ 单调棘轮，锚点重锚 6 次
     SL 一步不跟（距现价 400 → 536 点，超出 300~500 口径）
  ② SL 抖动：already_full 时按重算的 `want`（结构位每根 K 线都变）算 target，
     保护着并不存在的档位 → 700 轮重挂 24 次
  ③ 空梯不 prune：`size > 0` 过滤把负 size 的空头单全排除 → 重锚后阶梯卡在旧价位
  ④ 档位抖动：`want` 每轮重算 → prune 撤一条补一条，阶梯长度 1~12 条震荡
  ⑤ always_on 失效：`execute_signal` 在 `intents` 为空时提前 return →
     `_complete_ladder` 永不执行，0 单轮阶梯/保护单全空
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent, SignalFile  # noqa: E402

MARK = 2332.71
ATR = 7.5


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK, pos=0.0, pos_side="long"):
        self.mark, self.pos, self.pos_side = mark, pos, pos_side
        self.book, self.prot = [], []
        self.placed = 0
        self.prot_placed = 0

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if not self.pos:
            return []
        return [{"contract": "ETH_USDT", "side": self.pos_side,
                 "size": str(self.pos), "entry_price": str(self.mark)}]

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        rec = dict(body, id=f"o{self.placed}", price=float(body["price"]),
                   size=float(body["size"]))
        self.placed += 1
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        rec = {"id": f"p{self.prot_placed}", "status": "open",
               "initial": body["initial"], "trigger": body["trigger"]}
        self.prot_placed += 1
        self.prot.append(rec)
        return {"id": rec["id"]}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, oid):
        self.prot = [p for p in self.prot if p["id"] != oid]
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
                 ATR, MARK, MARK * 1.013, MARK * 0.987))
    con.commit()
    con.close()
    return f.name


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = _db()

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def _tpl(self, **over):
        tpl = {
            "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
            "martin_slots": {3: 6, 7: 7, 10: 9}, "snap_tol_pct": 0.15, "martin": [],
            "always_on": True, "symbol": "ETH_USDT", "sl_pct": 5.0,
            "anchor_mode": "fixed", "reanchor_threshold_pct": 2.0,
            "anchor_state_file": str(self.dir / "anchor.json"),
            "levels_state_file": str(self.dir / "levels.json"),
            "sl_distance": {"enabled": True, "min_points": 300, "max_points": 500,
                            "range_mult": 4.0},
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5,
                        "bb_div": 6.0, "min_step_pct": 0.45, "max_step_pct": "auto",
                        "max_span_pct": 6.0, "scale_martin": True,
                        "max_martin_pct": 9.0, "db_path": self.db},
        }
        tpl.update(over)
        return tpl

    def _run(self, client, tpl, meta=None, action=None):
        ex = Executor(client, symbols_whitelist=["ETH_USDT"], ladder_template=dict(tpl))
        ex.ladder_template = tpl
        ex._round_meta = dict(meta or {})
        rep = Report()
        intents = []
        if action:
            intents = [Intent(action=action, symbol="ETH_USDT",
                              price=client.mark * (0.995 if action.endswith("long")
                                                   else 1.005),
                              size_usd=client.mark * 0.01)]
        ex._complete_ladder(intents, rep)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        det = next((d for d in dets if d.get("blocked") or d.get("skipped")
                    or "step_pct" in d), dets[0] if dets else {})
        return ex, det, rep


class TestSlFollowsAnchor(Base):
    """① 空头 SL 必须跟着重锚走，不是单调棘轮。"""

    def test_sl_moves_down_with_reanchor(self):
        c = Client(MARK, 0.0, "short")
        tpl = self._tpl()
        sls = []
        for mark in (2390.0, 2300.0, 2210.0):
            c.mark = mark
            _, det, _ = self._run(c, tpl, action="open_short")
            self.assertTrue(c.prot, f"mark={mark} 必须有保护单")
            sl = float(c.prot[-1]["trigger"]["price"])
            book = [float(b["price"]) for b in c.book
                    if float(b["size"]) < 0]
            if book:
                deepest = max(book)
                gap = abs(sl - deepest)
                self.assertTrue(280 <= gap <= 540,
                                f"mark={mark} SL={sl} 最深档={deepest} 间距={gap:.1f}"
                                f" 超出 300~500 口径")
            sls.append(sl)
        self.assertTrue(sls[0] > sls[-1],
                        f"价格从 2390 跌到 2210，SL 必须跟着下移：{sls}")
        self.assertLess(abs(sls[-1] - sls[0]), 400,
                        f"SL 未能跟上阶梯（棘轮）：{sls}")


class TestAlreadyFullSlUsesBook(Base):
    """② already_full 时按盘上档位算 SL target，结构位抖动不该让 SL 重挂。"""

    def test_structure_jitter_does_not_rewrite_sl(self):
        c = Client(MARK, 0.0, "long")
        tpl = self._tpl(levels_dynamic={"enabled": True, "interval": "15m",
                                        "lookback": 96, "min_levels": 10,
                                        "max_levels": 15, "structure_only": True,
                                        "min_gap_pct": 0.3})
        lv_sets = [
            [2320.0, 2300.0, 2280.0, 2260.0, 2240.0, 2220.0, 2200.0, 2180.0,
             2160.0, 2140.0],
            [2325.0, 2303.0, 2284.0, 2262.0, 2243.0, 2221.0, 2202.0, 2183.0,
             2161.0, 2141.0],
            [2318.0, 2298.0, 2278.0, 2258.0, 2238.0, 2218.0, 2198.0, 2178.0,
             2158.0, 2138.0],
        ]
        for lv in lv_sets:
            _, det, _ = self._run(c, tpl, meta={"structure_levels": lv},
                                  action="open_long")
        first = len(c.prot)
        before = [(p["trigger"]["price"], p["initial"]["size"]) for p in c.prot]
        for _ in range(3):
            for lv in lv_sets:
                _, det, _ = self._run(c, tpl, meta={"structure_levels": lv},
                                      action="open_long")
        after = [(p["trigger"]["price"], p["initial"]["size"]) for p in c.prot]
        self.assertEqual(len(c.prot), first,
                         f"SL 不应增殖：{first} → {len(c.prot)}")
        self.assertEqual(before, after,
                         f"结构位小幅抖动不该换 SL：{before} → {after}")


class TestShortPrune(Base):
    """③ 空头挂单（size < 0）必须能被模板 prune。"""

    def test_off_template_short_orders_removed(self):
        c = Client(MARK, 0.0, "short")
        # 手工铺一批**旧锚点**的空头档位（远离模板档位）
        for i, px in enumerate([2600.0, 2620.0, 2640.0, 2660.0, 2680.0]):
            c.book.append({"id": f"x{i}", "price": px, "size": -1.0})
        tpl = self._tpl()
        _, det, rep = self._run(c, tpl, action="open_short")
        left = [float(b["price"]) for b in c.book]
        self.assertFalse(any(p > 2500 for p in left),
                         f"旧锚点空头档位必须被撤：剩余 {sorted(left)}")
        removed = [r for r in rep.results
                   if (r.detail or {}).get("pruned_off_template")]
        self.assertTrue(removed, "必须留下 pruned_off_template 痕迹")

    def test_new_ladder_is_on_short_side(self):
        c = Client(MARK, 0.0, "short")
        for i, px in enumerate([2600.0, 2620.0]):
            c.book.append({"id": f"x{i}", "price": px, "size": -1.0})
        tpl = self._tpl()
        _, _, _ = self._run(c, tpl, action="open_short")
        px = [float(b["price"]) for b in c.book]
        self.assertTrue(all(p > MARK for p in px),
                        f"空头档位必须全在现价上方：{sorted(px)}")


class TestLevelsStable(Base):
    """④ fixed 锚点没重锚 → 档位不重挂（结构位/档距抖动不该撤了又补）。"""

    def test_no_rewrite_when_anchor_unchanged(self):
        c = Client(MARK, 0.0, "long")
        tpl = self._tpl(levels_dynamic={"enabled": True, "interval": "15m",
                                        "lookback": 96, "min_levels": 10,
                                        "max_levels": 15, "structure_only": True,
                                        "min_gap_pct": 0.3})
        lv1 = [2320.0, 2300.0, 2280.0, 2260.0, 2240.0, 2220.0, 2200.0, 2180.0,
               2160.0, 2140.0]
        lv2 = [2326.0, 2306.0, 2286.0, 2266.0, 2246.0, 2226.0, 2206.0, 2186.0,
               2166.0, 2146.0]
        self._run(c, tpl, meta={"structure_levels": lv1}, action="open_long")
        n1 = c.placed
        px1 = sorted(float(b["price"]) for b in c.book)
        self._run(c, tpl, meta={"structure_levels": lv2}, action="open_long")
        px2 = sorted(float(b["price"]) for b in c.book)
        self.assertEqual(c.placed, n1, f"锚点没变不该补挂：{n1} → {c.placed}")
        self.assertEqual(px1, px2, f"档位不该漂移：{px1[:3]} vs {px2[:3]}")

    def test_reanchor_does_rewrite(self):
        c = Client(MARK, 0.0, "long")
        tpl = self._tpl()
        self._run(c, tpl, action="open_long")
        px1 = sorted(float(b["price"]) for b in c.book)
        c.mark = MARK * 0.95           # 跌 5% > 2% 阈值 → 必须重锚
        _, det, _ = self._run(c, tpl, action="open_long")
        px2 = sorted(float(b["price"]) for b in c.book)
        self.assertNotEqual(px1, px2, "重锚后档位必须换")
        self.assertTrue(all(p < c.mark for p in px2),
                        f"重锚后多头档位必须在现价下方：{px2}")

    def test_bad_levels_cache_ignored(self):
        (self.dir / "levels.json").write_text(json.dumps({
            "ETH_USDT": {"side": "long", "anchor": MARK,
                         "levels": [[-1, 1], [0, 1], [2300.0, 1]]}}),
            encoding="utf-8")
        c = Client(MARK, 0.0, "long")
        _, _, _ = self._run(c, self._tpl(), action="open_long")
        px = [float(b["price"]) for b in c.book]
        self.assertTrue(all(p > 0 for p in px), f"负价/0 价档位不能落地：{px}")
        self.assertGreaterEqual(len(px), 9, f"坏缓存应回退重算：{len(px)} 层")


class TestAlwaysOnChain(Base):
    """⑤ 0 单信号必须走完铺梯（execute_signal 全链路）。"""

    def test_empty_signal_lays_ladder(self):
        c = Client(MARK)
        tpl = self._tpl()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
        ex.ladder_template = tpl
        rep = ex.execute_signal(SignalFile(intents=[], meta={}, raw={}, replace="none"))
        errs = [r.error for r in rep.results if getattr(r, "error", None)]
        self.assertFalse(errs, f"0 单轮不该报错：{errs}")
        self.assertGreaterEqual(len(c.book), 9,
                                f"always_on 0 单轮必须铺出阶梯，实际 {len(c.book)} 层")
        self.assertTrue(c.prot, "0 单轮必须挂保护单")

    def test_always_on_off_keeps_noop(self):
        c = Client(MARK)
        tpl = self._tpl(always_on=False)
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
        ex.ladder_template = tpl
        ex.execute_signal(SignalFile(intents=[], meta={}, raw={}, replace="none"))
        self.assertEqual(len(c.book), 0, "always_on=false 时 0 单轮不该铺梯")

    def test_empty_signal_idempotent(self):
        c = Client(MARK)
        tpl = self._tpl()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
        ex.ladder_template = tpl
        for _ in range(4):
            ex.execute_signal(SignalFile(intents=[], meta={}, raw={}, replace="none"))
        self.assertEqual(len(c.book), len({float(b["price"]) for b in c.book}),
                         "连跑 4 轮不该出现同价重复挂单")
        self.assertEqual(len(c.prot), 1, f"SL 不该增殖：{len(c.prot)} 条")


if __name__ == "__main__":
    unittest.main()
