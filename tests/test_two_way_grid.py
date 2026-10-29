# -*- coding: utf-8 -*-
"""双向网格 2.0 / 3.0：引擎层双向 + **趋势加权**（非对称）。

## 两代的关系

**2.0**（632629c）= 引擎同时维护两侧，`_SideClient` 把 client 投影成单侧视图，
两侧互不 prune、SL 各管各的。解决的是「1.0 靠模型挂两边、一开模板就被撤掉」。

**3.0**（本文件）= 在 2.0 之上加**趋势权重**：双向不是对称铺满。
大趋势那一侧多挂 / 密 / 重，逆势那侧少挂 / 稀 / 轻，极端时干脆不挂。

    dev    = (close − ema200) / ema200
    slope  = ema200 近 lookback 根变化率
    trend  = 0.6·clip(dev/full) + 0.4·clip(slope/full)   ∈ [−1,1]
    w_long  = 0.5 + 0.5·trend      w_short = 0.5 − 0.5·trend
    levels  = base·(0.35 + 0.65·w)          # 逆势侧层数少
    step    = dynamic_step·(1.4 − 0.4·w)    # 逆势侧间距大
    lots    = base·(0.30 + 0.70·w)          # 逆势侧仓位轻
    w < min_weight → 该侧不挂

注意 `w_long + w_short = 1`，所以两侧**不可能都满配** —— 原先「两侧各 10 层」
的断言已不成立，改成「按权重不对称，且方向正确」。
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

MARK = 2332.71
ATR = 7.5


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK):
        self.mark = mark
        self.book, self.prot = [], []
        self.placed, self.cancelled = [], []
        self.n = 0

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        rec = dict(body, id=f"o{self.n}", price=float(body["price"]),
                   size=float(body["size"]))
        self.n += 1
        self.book.append(rec)
        self.placed.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        rec = {"id": f"p{self.n}", "status": "open",
               "initial": body["initial"], "trigger": body["trigger"]}
        self.n += 1
        self.prot.append(rec)
        return {"id": rec["id"]}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
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
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL)")
    half = MARK * 2.6 / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 ATR, MARK, MARK + half, MARK - half, None))
    con.commit()
    con.close()
    return f.name


#: 中性 4H：close == EMA200 且 EMA 走平 → trend=0 → 两侧各 0.5 权重
NEUTRAL_4H = [(MARK, MARK), (MARK, MARK), (MARK, MARK)]
#: 空头趋势（中等）：close 低于 EMA200 约 1.7%、EMA200 缓跌 → trend≈−0.39
#: → w_long≈0.30 / w_short≈0.70，两侧都挂但明显不对称
BEAR_4H = [(2380.0, 2390.0), (2360.0, 2385.0), (2340.0, 2380.0)]
#: 多头趋势（中等）：镜像 → w_long≈0.70 / w_short≈0.30
BULL_4H = [(2280.0, 2270.0), (2300.0, 2275.0), (2320.0, 2280.0)]
#: 极强空：close 低于 EMA200 约 6.8% 且 EMA200 快速下行 → w_long < 0.15 → 不挂
HARD_BEAR_4H = [(2100.0, 2400.0), (2150.0, 2380.0), (2200.0, 2360.0)]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = _db()

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def add_4h(self, rows):
        """写 4H 数据（**旧→新**）。close 与 ema200 都要给，斜率靠相邻差算。"""
        con = sqlite3.connect(self.db)
        base_t = 1793170000 - len(rows) * 14400
        for i, (c1, e1) in enumerate(rows):
            con.execute(
                "INSERT INTO kline (symbol, interval, t, closed, o, h, l, c, ema200) "
                "VALUES ('ETH_USDT','4h',?,1,?,?,?,?,?)",
                (base_t + i * 14400, c1, c1 + 5, c1 - 5, c1, e1))
        con.commit()
        con.close()

    def tpl(self, two_way=True, cfg=None, **over):
        t = {
            "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
            "martin_slots": {3: 6, 7: 7, 10: 9}, "snap_tol_pct": 0.15, "martin": [],
            "always_on": True, "symbol": "ETH_USDT", "sl_pct": 5.0,
            "anchor_mode": "fixed", "reanchor_threshold_pct": 2.0,
            "anchor_state_file": str(self.dir / "anchor.json"),
            "levels_state_file": str(self.dir / "levels.json"),
            "sl_distance": {"enabled": True, "min_points": 300,
                            "max_points": 500, "range_mult": 4.0},
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5,
                        "bb_div": 6.0, "min_step_pct": 0.45, "max_step_pct": "auto",
                        "max_span_pct": 6.0, "scale_martin": True,
                        "max_martin_pct": 9.0, "db_path": self.db},
        }
        if two_way:
            t["two_way"] = True
            t["two_way_sides"] = ["long", "short"]
            t["two_way_cfg"] = dict({
                "enabled": True, "min_weight": 0.15, "levels_at_min": 0.35,
                "step_at_min": 1.4, "lots_at_min": 0.3, "levels_max": 15,
                "dev_full_pct": 3.0, "slope_full_pct": 3.0, "trend_lookback": 3,
            }, **(cfg or {}))
        t.update(over)
        return t

    def run_round(self, c, tpl, meta=None):
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
        ex.ladder_template = tpl
        ex._round_meta = dict(meta or {})
        rep = Report()
        ex._complete_ladder([], rep)
        return ex, rep

    @staticmethod
    def sides(c):
        longs = sorted(((float(b["price"]), abs(float(b["size"])))
                        for b in c.book if float(b["size"]) > 0), reverse=True)
        shorts = sorted(((float(b["price"]), abs(float(b["size"])))
                         for b in c.book if float(b["size"]) < 0))
        return longs, shorts

    @staticmethod
    def gap_pct(px):
        if len(px) < 2:
            return 0.0
        return abs(px[0][0] - px[-1][0]) / min(px[0][0], px[-1][0]) * 100 / (len(px) - 1)

    @staticmethod
    def sl_of(c, rule):
        out = []
        for p in c.prot:
            trg = p.get("trigger") or {}
            try:
                r = int(trg.get("rule") or 0)
            except (TypeError, ValueError):
                r = 0
            if r == rule:
                out.append((float(trg.get("price") or 0),
                            float((p.get("initial") or {}).get("size") or 0)))
        return out

    @staticmethod
    def plans(rep):
        """从结果里挖出每侧的 side_plan。"""
        out = {}
        for r in rep.results:
            d = getattr(r, "detail", None) or {}
            sd = d.get("two_way_side")
            plan = d.get("side_plan")
            if sd and plan:
                out[sd] = plan
        return out


class TestBothSides(Base):
    """2.0 基础：两侧同时维护、方向正确、互不干扰。"""

    def test_both_sides_lay(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        self.assertGreaterEqual(len(longs), 5, f"中性趋势多侧也应有货：{len(longs)}")
        self.assertGreaterEqual(len(shorts), 5, f"中性趋势空侧也应有货：{len(shorts)}")
        self.assertTrue(all(p < MARK for p, _ in longs), f"多档必须在下方：{longs[:2]}")
        self.assertTrue(all(p > MARK for p, _ in shorts), f"空档必须在上方：{shorts[:2]}")

    def test_martin_slots_present_both_sides(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        self.assertGreater(max(s for _, s in longs), 1, "多侧应有放大档（马丁）")
        self.assertGreater(max(s for _, s in shorts), 1, "空侧应有放大档（马丁）")
        self.assertEqual(max(s for _, s in longs), max(s for _, s in shorts),
                         "两侧马丁放大倍数应一致（张数缩放因子相同）")

    def test_neutral_steps_symmetric(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        gl, gs = self.gap_pct(longs), self.gap_pct(shorts)
        self.assertLess(abs(gl - gs), 0.05,
                        f"中性趋势两侧档距应近似相等：多 {gl:.4f}% vs 空 {gs:.4f}%")

    def test_single_side_mode_unchanged(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl(two_way=False, direction_mode="signal"))
        longs, shorts = self.sides(c)
        self.assertEqual(len(shorts), 0, "单向模式不该有空单")
        self.assertEqual(len(longs), 10, f"单向应铺满 10 层：{len(longs)}")


class TestTrendAsymmetry(Base):
    """3.0 核心：趋势决定两侧的不对称。"""

    def test_bear_market_short_side_heavier(self):
        self.add_4h(BEAR_4H)
        c = Client()
        _, rep = self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        nl, ns = len(longs), len(shorts)
        self.assertGreater(ns, nl, f"大趋势空：空侧层数应更多（空 {ns} vs 多 {nl}）")
        gl, gs = self.gap_pct(longs), self.gap_pct(shorts)
        self.assertGreater(gl, gs,
                           f"大趋势空：多侧间距应更宽（多 {gl:.4f}% vs 空 {gs:.4f}%）")
        self.assertGreater(sum(s for _, s in shorts), sum(s for _, s in longs),
                           "大趋势空：空侧总张数应更大")
        plans = self.plans(rep)
        if plans:
            self.assertLess(plans["long"]["weight"], plans["short"]["weight"])

    def test_bull_market_long_side_heavier(self):
        self.add_4h(BULL_4H)
        c = Client()
        _, rep = self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        nl, ns = len(longs), len(shorts)
        self.assertGreater(nl, ns, f"大趋势多：多侧层数应更多（多 {nl} vs 空 {ns}）")
        gl, gs = self.gap_pct(longs), self.gap_pct(shorts)
        self.assertGreater(gs, gl,
                           f"大趋势多：空侧间距应更宽（空 {gs:.4f}% vs 多 {gl:.4f}%）")
        self.assertGreater(sum(s for _, s in longs), sum(s for _, s in shorts),
                           "大趋势多：多侧总张数应更大")

    def test_neutral_is_balanced(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        self.assertLessEqual(abs(len(longs) - len(shorts)), 1,
                             f"震荡应基本均衡：多 {len(longs)} vs 空 {len(shorts)}")

    def test_extreme_trend_can_skip_side(self):
        """权重低于 min_weight → 该侧完全不挂（用户「甚至不挂」）。"""
        self.add_4h(NEUTRAL_4H)
        c = Client()
        _, rep = self.run_round(c, self.tpl(),
                                meta={"two_way": {"long": {"weight": 0.05}}})
        longs, shorts = self.sides(c)
        self.assertEqual(len(longs), 0, f"多侧权重 0.05 < 0.15 应不挂：{longs}")
        self.assertGreater(len(shorts), 0, "空侧不受影响，仍应铺")
        skipped = [r.detail for r in rep.results
                   if (getattr(r, "detail", None) or {}).get("skipped") == "side_weight_zero"]
        self.assertTrue(skipped, "必须留 side_weight_zero 痕迹")

    def test_hard_trend_skips_counter_side(self):
        """极强趋势：逆势侧权重低于阈值 → 自动不挂（不需要 AI 指定）。"""
        self.add_4h(HARD_BEAR_4H)
        c = Client()
        _, rep = self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        self.assertEqual(len(longs), 0, f"极强空下多侧应自动不挂：{longs}")
        self.assertGreater(len(shorts), 0, "空侧仍应铺")
        skipped = [r.detail for r in rep.results
                   if (getattr(r, "detail", None) or {}).get("skipped") == "side_weight_zero"]
        self.assertTrue(skipped, "应留 side_weight_zero 痕迹")

    def test_trend_weights_sum_to_one(self):
        for rows in (NEUTRAL_4H, BEAR_4H, BULL_4H):
            if rows is not NEUTRAL_4H:
                con = sqlite3.connect(self.db)
                con.execute("DELETE FROM kline WHERE interval='4h'")
                con.commit()
                con.close()
            self.add_4h(rows)
            tpl = self.tpl()
            ex = Executor(Client(), symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
            ex.ladder_template = tpl
            w, note = ex._trend_weight("ETH_USDT", tpl)
            self.assertAlmostEqual(w["long"] + w["short"], 1.0, places=6,
                                   msg=f"两侧权重应守恒：{w}（{note}）")
            self.assertGreaterEqual(min(w["long"], w["short"]), 0.0)
            self.assertLessEqual(max(w["long"], w["short"]), 1.0)


class TestAiOverride(Base):
    """AI 显式参数优先于趋势映射。"""

    def test_ai_levels_beat_trend(self):
        self.add_4h(BEAR_4H)
        c = Client()
        self.run_round(c, self.tpl(), meta={"two_way": {"long": {"levels": 3}}})
        longs, _ = self.sides(c)
        self.assertEqual(len(longs), 3, f"AI 指定 3 层应照用：{len(longs)}")

    def test_ai_step_beat_trend(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl(), meta={"two_way": {"long": {"step_pct": 0.9}}})
        longs, _ = self.sides(c)
        g = self.gap_pct(longs)
        self.assertLess(abs(g - 0.9), 0.08, f"AI 指定 0.9% 档距应生效：{g:.4f}%")

    def test_ai_weight_beats_trend(self):
        self.add_4h(BEAR_4H)
        c = Client()
        _, rep = self.run_round(c, self.tpl(),
                                meta={"two_way": {"long": {"weight": 1.0}}})
        plans = self.plans(rep)
        if plans:
            self.assertAlmostEqual(plans["long"]["weight"], 1.0, places=2)
            self.assertIn("ai_weight", plans["long"]["weight_src"])


class TestIsolationAndProtection(Base):
    """两侧隔离 + 各自 SL。"""

    def test_prune_one_side_keeps_other(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl())
        _, shorts_before = self.sides(c)
        c.book.append({"id": "bad_long", "price": 9999.0, "size": 1.0})
        self.run_round(c, self.tpl())
        self.assertIn("bad_long", c.cancelled, "多侧非模板档必须撤")
        _, shorts_after = self.sides(c)
        self.assertEqual(shorts_before, shorts_after,
                         f"空侧不该被多侧 prune 波及：{shorts_before} → {shorts_after}")

    def test_sl_rules_are_opposite(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl())
        long_sl, short_sl = self.sl_of(c, 2), self.sl_of(c, 1)
        self.assertEqual(len(long_sl), 1, f"多头应有 1 条 rule=2 SL：{long_sl}")
        self.assertEqual(len(short_sl), 1, f"空头应有 1 条 rule=1 SL：{short_sl}")
        self.assertLess(long_sl[0][0], MARK, "多单 SL 在下方（跌破卖出）")
        self.assertGreater(short_sl[0][0], MARK, "空单 SL 在上方（涨破买入）")
        self.assertLess(long_sl[0][1], 0, "多单 SL 是卖单（负）")
        self.assertGreater(short_sl[0][1], 0, "空单 SL 是买单（正）")

    def test_sl_covers_own_side_only(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        n_long = int(sum(s for _, s in longs))
        n_short = int(sum(s for _, s in shorts))
        self.assertEqual(abs(self.sl_of(c, 2)[0][1]), n_long,
                         f"多单 SL 只覆盖多侧 {n_long} 张")
        self.assertEqual(abs(self.sl_of(c, 1)[0][1]), n_short,
                         f"空单 SL 只覆盖空侧 {n_short} 张")


class TestStability(Base):
    def test_five_rounds_idempotent(self):
        """连跑 5 轮：档位零漂移、不重复挂、SL 恒 2 条。"""
        self.add_4h(NEUTRAL_4H)
        c = Client()
        tpl = self.tpl()
        snaps = []
        for _ in range(5):
            self.run_round(c, tpl)
            longs, shorts = self.sides(c)
            snaps.append((len(c.placed),
                          tuple(round(p, 2) for p, _ in longs),
                          tuple(round(p, 2) for p, _ in shorts),
                          len(c.prot)))
        for i, s in enumerate(snaps):
            self.assertEqual(s[0], snaps[0][0], f"第{i+1}轮不该再补挂")
            self.assertEqual(s[1], snaps[0][1], f"第{i+1}轮多侧档位漂移")
            self.assertEqual(s[2], snaps[0][2], f"第{i+1}轮空侧档位漂移")
            self.assertEqual(s[3], 2, f"第{i+1}轮 SL 应恒为 2 条，实际 {s[3]}")

    def test_reanchor_moves_both_sides(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        tpl = self.tpl()
        self.run_round(c, tpl)
        lb, sb = self.sides(c)
        c.mark = MARK * 0.95
        self.run_round(c, tpl)
        la, sa = self.sides(c)
        self.assertNotEqual([p for p, _ in lb], [p for p, _ in la], "重锚后多侧必须换档")
        self.assertNotEqual([p for p, _ in sb], [p for p, _ in sa], "重锚后空侧必须同样换档")
        self.assertTrue(all(p < c.mark for p, _ in la), "重锚后多档仍在现价下方")
        self.assertTrue(all(p > c.mark for p, _ in sa), "重锚后空档仍在现价上方")

    def test_levels_state_split_by_side(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        self.run_round(c, self.tpl())
        data = json.loads((self.dir / "levels.json").read_text(encoding="utf-8"))
        self.assertIn("ETH_USDT:long", data, f"档位 state 应按侧分开：{list(data)}")
        self.assertIn("ETH_USDT:short", data, f"档位 state 应按侧分开：{list(data)}")

    def test_structure_levels_used_per_side(self):
        self.add_4h(NEUTRAL_4H)
        c = Client()
        tpl = self.tpl(levels_dynamic={"enabled": True, "interval": "15m",
                                       "lookback": 96, "min_levels": 10,
                                       "max_levels": 15, "structure_only": True,
                                       "min_gap_pct": 0.3})
        lv = [2300.0, 2280.0, 2260.0, 2240.0, 2220.0,
              2370.0, 2390.0, 2410.0, 2430.0, 2450.0]
        self.run_round(c, tpl, meta={"structure_levels": lv})
        longs, shorts = self.sides(c)
        self.assertTrue(any(abs(p - 2300.0) < 0.5 for p, _ in longs),
                        f"多侧应吸附下方结构位 2300：{[p for p, _ in longs]}")
        self.assertTrue(any(abs(p - 2370.0) < 0.5 for p, _ in shorts),
                        f"空侧应吸附上方结构位 2370：{[p for p, _ in shorts]}")
        self.assertTrue(all(p < MARK for p, _ in longs))
        self.assertTrue(all(p > MARK for p, _ in shorts))


if __name__ == "__main__":
    unittest.main()
