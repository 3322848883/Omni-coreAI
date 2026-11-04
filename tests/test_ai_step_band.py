# -*- coding: utf-8 -*-
"""AI 档距 vs ATR/BB 动态档距（`dynamic.ai_step_mode`，2026-11-02）。

用户口径：「档距要**结合布林带与 ATR 动态调整**，并落在价距范围内」。

改之前实际是：AI 每轮都给 `meta.two_way.<side>.step_pct`（实测 3/3），而优先级是
「AI > 动态公式」→ **动态公式从未生效**（raw 0.702% 被 AI 的 0.8 直接盖掉）。
本文件把三种模式钉住：

  override（旧行为）  AI 说了算，只受 [min_step_pct, max_step_pct] 硬夹
  band                AI 只能落在**动态值 ±ai_step_band** 内
  ignore              完全用动态值，AI 的建议只留痕
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402

MARK = 2332.71
ATR_POINTS = 6.53          # 相对 mark ≈ 0.28% → ×2.5 = 0.70%（动态基准）


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


def _bars(n=60, mark=MARK):
    """构造 K 线：每根 h-l 固定 = ATR_POINTS，收盘几乎不动（BB 带宽很小）。"""
    out = []
    for i in range(n):
        c = mark + (i % 3 - 1) * 0.2      # 微幅波动 → sd 极小 → bw/6 可忽略
        out.append({"t": 1793170000 + i * 900, "o": c, "h": c + ATR_POINTS / 2,
                    "l": c - ATR_POINTS / 2, "c": c, "v": 100.0})
    return out


class Client:
    def __init__(self):
        self.book = []
        self.prot = []

    def get_last_price(self, sym):
        return MARK

    def get_contract(self, sym):
        return Meta()

    def get_klines(self, sym, interval, limit=100):
        return _bars()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-sb"}
                for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        oid = f"o{len(self.book)}"
        self.book.append(dict(body, id=oid))
        return {"id": oid}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        return {}

    def cancel_price_order(self, pid):
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
    con.commit()
    con.close()
    return f.name


def _levels(n=20, start=0.3, step=0.4):
    return [round(MARK * (1 - (start + i * step) / 100.0), 2) for i in range(n)]


class Base(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def tpl(self, mode=None, band=None, min_step=0.3):
        t = {
            "enabled": True, "two_way": True, "two_way_sides": ["long"],
            "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
            "martin_slots": {1: 3, 5: 4, 10: 6}, "martin_slots_by_index": True,
            "snap_tol_pct": 0.15, "martin": [], "always_on": True,
            "symbol": "ETH_USDT", "sl_pct": 5.0, "anchor_mode": "fixed",
            "anchor_state_file": str(self.tmp / "a.json"),
            "levels_state_file": str(self.tmp / "l.json"),
            "levels_dynamic": {"enabled": True, "min_levels": 10, "max_levels": 10,
                               "structure_only": True, "min_gap_pct": 0.15,
                               "auto_extract": False},
            "two_way_cfg": {"enabled": True, "min_weight": 0, "levels_at_min": 1.0,
                            "lots_at_min": 1.0, "step_at_min": 1.0, "levels_floor": 10},
            "sl_distance": {"enabled": True, "min_points": 300, "max_points": 320},
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 2.5,
                        "bb_div": 6.0, "min_step_pct": min_step, "max_step_pct": 2.2,
                        "max_span_pct": 25.0, "db_path": self.db},
        }
        if mode:
            t["dynamic"]["ai_step_mode"] = mode
        if band is not None:
            t["dynamic"]["ai_step_band"] = band
        return t

    def run_round(self, ai_step, mode=None, band=None, min_step=0.3):
        c = Client()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], root=self.tmp, bot_id="sb")
        ex.ladder_template = self.tpl(mode=mode, band=band, min_step=min_step)
        ex._round_meta = {
            "two_way": {"long": {"weight": 0.6, "levels": 10,
                                 "step_pct": ai_step, "lots": 1}},
            "structure_levels": _levels(),
            "ladder_step_pct": ai_step,
        }
        rep = Report()
        ex._forced_side = "long"
        ex._complete_ladder([], rep)
        det = next((s.detail for s in rep.results
                    if getattr(s, "action", "") == "ladder_template" and s.detail), {})
        return det, det.get("dynamic") or {}


class TestOverrideMode(Base):
    def test_ai_step_wins(self):
        det, dyn = self.run_round(ai_step=2.0)
        self.assertAlmostEqual(det["step_pct"], 2.0, places=3,
                               msg="override 模式下 AI 的档距直接生效")
        self.assertIsNone(dyn.get("ai_step_band"), "override 不该留 band 痕迹")

    def test_dynamic_value_still_computed(self):
        _, dyn = self.run_round(ai_step=2.0)
        self.assertIsNotNone(dyn.get("raw_step_pct"),
                             "即使不采用，动态值也必须算出来并留痕（否则无法复盘）")
        self.assertAlmostEqual(dyn["raw_step_pct"], 0.70, delta=0.06)


class TestBandMode(Base):
    def test_ai_too_wide_is_capped(self):
        det, dyn = self.run_round(ai_step=2.0, mode="band", band=0.3)
        ref = dyn["ai_step_dyn_ref"]
        self.assertAlmostEqual(det["step_pct"], ref * 1.3, places=3,
                               msg="AI 给得比动态值宽 30% 以上 → 压到上限")
        self.assertTrue(dyn.get("ai_step_clamped"))

    def test_ai_too_narrow_is_lifted(self):
        det, dyn = self.run_round(ai_step=0.3, mode="band", band=0.3)
        ref = dyn["ai_step_dyn_ref"]
        self.assertAlmostEqual(det["step_pct"], ref * 0.7, places=3,
                               msg="AI 给得太窄 → 抬到下限（再被 min_step 夹）")

    def test_ai_inside_band_kept(self):
        det, dyn = self.run_round(ai_step=0.75, mode="band", band=0.3)
        ref = dyn["ai_step_dyn_ref"]
        self.assertTrue(ref * 0.7 <= 0.75 <= ref * 1.3, "前提：0.75 应落在带内")
        self.assertAlmostEqual(det["step_pct"], 0.75, places=3)

    def test_band_recorded(self):
        _, dyn = self.run_round(ai_step=2.0, mode="band", band=0.3)
        lo, hi = dyn["ai_step_band"]
        self.assertLess(lo, hi)
        self.assertAlmostEqual(lo, dyn["ai_step_dyn_ref"] * 0.7, places=3)
        self.assertAlmostEqual(hi, dyn["ai_step_dyn_ref"] * 1.3, places=3)

    def test_min_step_still_binds(self):
        """min_step 0.66 与 band 同时存在时，取更严的那个。"""
        det, dyn = self.run_round(ai_step=0.3, mode="band", band=0.3, min_step=0.66)
        self.assertGreaterEqual(det["step_pct"], 0.66 - 1e-9,
                                "硬下限不能被 band 突破")


class TestIgnoreMode(Base):
    def test_dynamic_wins(self):
        det, dyn = self.run_round(ai_step=2.0, mode="ignore")
        self.assertAlmostEqual(det["step_pct"], dyn["ai_step_dyn_ref"], places=3,
                               msg="ignore 模式下完全用动态值")
        self.assertEqual(dyn.get("ai_step_requested"), 2.0, "AI 建议仍需留痕")


class TestNoDynamic(Base):
    def test_band_ignored_when_dynamic_unavailable(self):
        """动态值算不出来（库无 boll 列 + client 没 K 线）→ band 不生效，回 override。"""
        c = Client()
        c.get_klines = lambda *a, **k: []
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], root=self.tmp, bot_id="sb")
        tpl = self.tpl(mode="band", band=0.3)
        tpl["dynamic"]["db_path"] = self.db          # 空表 → no_indicator_row
        ex.ladder_template = tpl
        ex._round_meta = {
            "two_way": {"long": {"weight": 0.6, "levels": 10, "step_pct": 1.5,
                                 "lots": 1}},
            "structure_levels": _levels(),
            "ladder_step_pct": 1.5,
        }
        rep = Report()
        ex._forced_side = "long"
        ex._complete_ladder([], rep)
        det = next((s.detail for s in rep.results
                    if getattr(s, "action", "") == "ladder_template" and s.detail), {})
        self.assertAlmostEqual(det["step_pct"], 1.5, places=3,
                               msg="动态不可用时不该凭空造 band")


if __name__ == "__main__":
    unittest.main()


class TestStructurePriorityOverStep(Base):
    """结构位优先：AI 的档距**不能**成为结构位的准入门槛（2026-11-02）。

    改之前 `min_gap_pct: auto` 取的是**当轮 step_pct**（整格 0.9% ≈ 20.5 点）当去重阈值，
    于是间距 0.7~0.8% 的密集结构位被整批丢掉 —— 实测多侧 8 个位只剩 3 个、其余 7 档
    全是补点。这与「结构位决定挂在哪、档距只用来补档」正好相反。
    """

    def _run(self, ai_step, gap_pct=0.7):
        c = Client()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], root=self.tmp, bot_id="sp")
        tpl = self.tpl(mode="band", band=0.3)
        tpl["levels_dynamic"]["min_gap_pct"] = "auto"      # 走 min_step 分支
        tpl["dynamic"]["min_step_pct"] = 0.66              # 生产口径的物理下限
        ex.ladder_template = tpl
        ex._round_meta = {
            "two_way": {"long": {"weight": 0.6, "levels": 10,
                                 "step_pct": ai_step, "lots": 1}},
            "structure_levels": [round(MARK * (1 - (0.3 + i * gap_pct) / 100.0), 2)
                                 for i in range(20)],
            "ladder_step_pct": ai_step,
        }
        rep = Report()
        ex._forced_side = "long"
        ex._complete_ladder([], rep)
        det = next((s.detail for s in rep.results
                    if getattr(s, "action", "") == "ladder_template" and s.detail), {})
        so = (det.get("dynamic") or {}).get("structure_only") or {}
        return det, so

    def test_dedup_threshold_is_min_step_not_ai_step(self):
        det, so = self._run(ai_step=0.9)
        self.assertAlmostEqual(so["min_gap_pct"], 0.66, places=3,
                               msg="去重阈值必须取 min_step_pct（物理下限），不是 AI 的 0.9%")
        self.assertEqual(so["min_gap_src"], "min_step")

    def test_dense_structure_levels_survive(self):
        """间距 0.7% 的位（> 0.66% 物理下限）应当全部可用，不该被 0.9% 的档距筛掉。"""
        det, so = self._run(ai_step=0.9)
        kept = so["in_side"] - so["deduped_out"]
        self.assertGreaterEqual(kept, 8,
                                f"间距 0.7% 的结构位应几乎全留，实际只留 {kept} 个（{so}）")
        self.assertLessEqual(so["deduped_out"], 1, f"最多丢 1 个：{so}")

    def test_fewer_fillers_needed(self):
        """结构位留得多 → 补点更少，档位更贴结构。"""
        det, so = self._run(ai_step=0.9)
        self.assertLessEqual(so["filled"], 4,
                             f"补点数应明显下降（原来 7 个），实际 {so['filled']}")
        self.assertEqual(so["levels"], 10)
