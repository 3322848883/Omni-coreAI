# -*- coding: utf-8 -*-
"""层数下限（`two_way_cfg.levels_floor`）+ 逐档止盈（`tp_by_unit`），2026-11-02。

两个真实缺口（本地真实行情实测，`plan` 3 轮 + `once` 落地）：

1. **层数被 AI 覆盖**：`_complete_ladder_one_side` 里
   `ld.min_levels/max_levels` 直接被 `meta.two_way.<side>.levels` 改写 →
   AI 给 5 层就只铺 5 层 → 马丁 `{1:3,5:4,10:6}` 的第 10 档（6 张）**永不出现**，
   总敞口从设计的 20 张掉到 13 张。`levels_floor` 把每侧抬到至少 N 层。

2. **逐档止盈没有输入**：`ensure_grid_tp` 只读 bot 级 `state/ladder_levels.json`，
   而那张表只有 `_record_ladder_level`（AI 发 `open_*` 腿）会写。模板独占入场后
   模型几乎总是 `hold` → 表为空 → `no_ladder_table` → **一张 TP 都不挂**。
   `tp_by_unit` 让模板铺梯时按 unit 落盘每档 tp_target。
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
    def __init__(self):
        self.book = []
        self.prot = []

    def get_last_price(self, sym):
        return MARK

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-tpg"}
                for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        self.book.append(dict(body, id=f"o{len(self.book)}"))
        return {"id": f"o{len(self.book)}"}

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
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 ATR, MARK, MARK * 1.013, MARK * 0.987))
    con.commit()
    con.close()
    return f.name


def _levels_below(n=20, start=0.3, step=0.4):
    """现价下方 n 个结构位（%→价），足够铺满 10 档。"""
    return [round(MARK * (1 - (start + i * step) / 100.0), 2) for i in range(n)]


class Base(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "data" / "bots" / "tpg" / "state"
        self.state.mkdir(parents=True)

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def tpl(self, floor=None, tp=True, include_short=False):
        t = {
            "enabled": True, "two_way": True,
            "two_way_sides": ["long", "short"] if include_short else ["long"],
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
                            "lots_at_min": 1.0, "step_at_min": 1.0},
            "sl_distance": {"enabled": True, "min_points": 300, "max_points": 320},
            "dynamic": {"enabled": False, "interval": "15m", "atr_mult": 2.5,
                        "bb_div": 6.0, "min_step_pct": 0.66, "max_step_pct": 2.2,
                        "max_span_pct": 25.0, "db_path": self.db},
        }
        if floor is not None:
            t["two_way_cfg"]["levels_floor"] = floor
        if tp:
            t["tp_by_unit"] = {"enabled": True, "grid_pct": 0,
                               "martin_pct": 0, "base_pct": 3.0}
        return t

    def run_round(self, ai_levels, floor=None, tp=True, side="long"):
        c = Client()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"],
                      root=self.tmp, bot_id="tpg")
        ex.ladder_template = self.tpl(floor=floor, tp=tp, include_short=(side == "short"))
        ex._round_meta = {
            "two_way": {side: {"weight": 0.6, "levels": ai_levels,
                               "step_pct": 0.5, "lots": 1}},
            "structure_levels": _levels_below() if side == "long"
            else [round(MARK * (1 + 0.3 / 100 + i * 0.004), 2) for i in range(20)],
            "ladder_step_pct": 0.5,
        }
        rep = Report()
        ex._forced_side = side
        ex._complete_ladder([], rep)
        legs = [(float(b["price"]), float(b["size"])) for b in c.book]
        longs = sorted([x for x in legs if x[1] > 0], reverse=True)
        shorts = sorted([x for x in legs if x[1] < 0])
        return (longs if side == "long" else shorts), rep, ex

    def table(self):
        p = self.state / "ladder_levels.json"
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8")).get("levels") or []


class TestLevelsFloor(Base):
    def test_no_floor_uses_ai_levels_plus_margin(self):
        """不配 floor 时是**既有**行为：AI 5 层 → min=5/max=n+3=8 → 实际 8 层。

        这条不是理想语义（AI 说 5 却铺 8），但先锁住现状，改动需单独评估。
        """
        rows, _, _ = self.run_round(ai_levels=5, floor=None)
        self.assertEqual(len(rows), 8, f"AI 5 层时既有行为铺 8 层，实际 {len(rows)}")

    def test_floor_lifts_to_ten_levels(self):
        rows, _, _ = self.run_round(ai_levels=5, floor=10)
        self.assertEqual(len(rows), 10, f"floor=10 必须铺满 10 层，实际 {len(rows)}")

    def test_floor_ten_gives_twenty_lots(self):
        rows, _, _ = self.run_round(ai_levels=5, floor=10)
        self.assertEqual(sum(s for _, s in rows), 20.0,
                         "10 层马丁位 1/5/10 = 3/4/6 张应合计 20 张，实际 %r" % (rows,))

    def test_martin_slots_hit_absolute_index(self):
        rows, _, _ = self.run_round(ai_levels=7, floor=10)
        self.assertEqual([s for _, s in rows], [3, 1, 1, 1, 4, 1, 1, 1, 1, 6],
                         "第 1/5/10 档必须是 3/4/6 张（绝对位次）")

    def test_ai_more_than_floor_kept(self):
        rows, _, _ = self.run_round(ai_levels=12, floor=10)
        self.assertEqual(len(rows), 12, "AI 要 12 层时 floor=10 不该往下压")

    def test_floor_recorded_in_note(self):
        _, rep, _ = self.run_round(ai_levels=5, floor=10)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        found = any((d.get("dynamic") or {}).get("levels_floor") for d in dets)
        self.assertTrue(found, "抬层数必须在 dyn_note 留痕（否则无法事后核对）")


class TestTpByUnit(Base):
    def test_table_written_when_enabled(self):
        self.run_round(ai_levels=10, floor=10, tp=True)
        lv = self.table()
        self.assertEqual(len(lv), 10, f"10 层应写 10 条档位，实际 {len(lv)}")
        self.assertTrue(all(x.get("unit") in ("grid", "martin", "base") for x in lv))

    def test_table_absent_when_disabled(self):
        self.run_round(ai_levels=10, floor=10, tp=False)
        self.assertEqual(self.table(), [], "tp_by_unit 关闭时不该写档位表")

    def test_unit_classification(self):
        self.run_round(ai_levels=10, floor=10)
        units = [x["unit"] for x in self.table()]
        self.assertEqual(units[0], "martin", "第 1 档 3 张 = 马丁位")
        self.assertEqual(units[4], "martin", "第 5 档 4 张 = 马丁位")
        self.assertEqual(units[9], "martin", "第 10 档 6 张 = 马丁位")
        self.assertEqual(units[1], "grid", "第 2 档 1 张 = 网格位")

    def test_long_tp_above_entry(self):
        self.run_round(ai_levels=10, floor=10)
        for x in self.table():
            self.assertGreater(x["tp_target"], x["price"],
                               f"多单 TP 必须在买入价上方：{x}")
            self.assertEqual(x["side"], "long")

    def test_tp_distance_by_unit(self):
        """网格吃一格、马丁吃两格（距离 = 当轮 step_pct，会被 min_step_pct 抬起）。"""
        _, rep, _ = self.run_round(ai_levels=10, floor=10)
        step = None
        for st in rep.results:
            if getattr(st, "action", "") == "ladder_template" and st.detail:
                step = float(st.detail.get("step_pct") or 0) or None
                if step:
                    break
        self.assertIsNotNone(step, "detail 必须带 step_pct")
        for x in self.table():
            dist = (x["tp_target"] - x["price"]) / x["price"] * 100
            want = 2.0 * step if x["unit"] == "martin" else step
            self.assertAlmostEqual(dist, want, delta=0.02,
                                   msg=f"{x['unit']} 档止盈距离应为 {want}%：{x}")

    def test_short_tp_below_entry(self):
        """空侧档 TP 必须在卖出价**下方**；`_complete_ladder` 会跑两侧，只看 short 子集。"""
        rows, _, _ = self.run_round(ai_levels=10, floor=10, side="short")
        lv = [x for x in self.table() if x.get("side") == "short"]
        self.assertEqual(len(lv), 10, f"空侧应写 10 条档位，实际 {len(lv)}")
        for x in lv:
            self.assertLess(x["tp_target"], x["price"], f"空单 TP 必须在卖出价下方：{x}")
            self.assertEqual(x["size"] > 0, True, "空侧档位 size 记正数（方向由 side 表达）")

    def test_resync_replaces_pending_keeps_closed(self):
        self.run_round(ai_levels=10, floor=10)
        # 手工把一档标成 closed，再跑一轮 → pending 全换、closed 保留
        p = self.state / "ladder_levels.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["levels"][0]["status"] = "closed"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.run_round(ai_levels=10, floor=10)
        lv = self.table()
        closed = [x for x in lv if x.get("status") == "closed"]
        pending = [x for x in lv if x.get("status") == "pending"]
        # 2026-11-04 用户口径（两段）：
        # ① 「成交一档就少一层」——成交档占额度，不再原地补满；
        # ② 「网格套利一次就释放一档」——止盈落袋（closed）**释放**额度，
        #    该档下一轮重新开档（回到 pending），梯子不会被越跑越稀。
        # 这里手工把一档标成 closed（= 套利完成）→ 下一轮应当重新开成 pending。
        self.assertEqual(len(pending), 10, "止盈落袋的档应重新开档（10 层满额）")
        self.assertEqual(len(closed), 0, "该档已被重新开档，不再停在 closed")


if __name__ == "__main__":
    unittest.main()


class TestChipMetaPromoted(unittest.TestCase):
    """模型把 `two_way` / `structure_levels` 写在 **chip.meta** 里也要能到引擎。

    2026-11-02 新网关第 1 轮实测：plan 根没有 `meta` 节点，全部塞在 `chips[i].meta`
    → `_pass_meta` 为空 → 引擎当"没给"，退化等距铺 10 层，整轮结构判断白丢。
    """

    def _plan(self, root_meta=None, chip_meta=None):
        from omnialpha.strategist.schema import Chip, Plan
        chip = Chip(symbol="ETH_USDT", action="hold", confidence=0.6,
                    reasoning="t")
        chip_raw = {"symbol": "ETH_USDT", "action": "hold", "confidence": 0.6,
                    "reasoning": "t"}
        if chip_meta is not None:
            chip_raw["meta"] = chip_meta
        raw = {"cycle_id": "c1", "reasoning": "r", "chips": [chip_raw]}
        if root_meta is not None:
            raw["meta"] = root_meta
        return Plan(cycle_id="c1", reasoning="r", chips=[chip], raw=raw)

    def _payload(self, plan):
        from omnialpha.strategist.bridge import chips_to_signal
        from omnialpha.strategist.risk import RiskResult
        rr = RiskResult(accepted=[], rejected=[], notes=[])
        # bridge 从 plan.chips 取 accepted；这里直接传空，只看 meta
        return chips_to_signal(plan, rr, bot_id="t")["meta"]

    def test_chip_meta_used_when_root_missing(self):
        plan = self._plan(chip_meta={
            "two_way": {"long": {"levels": 10, "step_pct": 0.8}},
            "structure_levels": [2200.0, 2210.0],
            "ladder_step_pct": 0.8,
        })
        meta = self._payload(plan)
        self.assertEqual(meta.get("ladder_step_pct"), 0.8)
        self.assertEqual(len(meta.get("structure_levels") or []), 2)
        self.assertIn("long", meta.get("two_way") or {})

    def test_root_meta_wins(self):
        plan = self._plan(
            root_meta={"structure_levels": [1000.0], "ladder_step_pct": 0.5},
            chip_meta={"structure_levels": [2000.0], "ladder_step_pct": 0.9})
        meta = self._payload(plan)
        self.assertEqual(meta.get("structure_levels"), [1000.0], "plan 级优先")
        self.assertEqual(meta.get("ladder_step_pct"), 0.5)

    def test_empty_root_filled_by_chip(self):
        """根 meta 存在但键为空 → 仍由 chip 补。"""
        plan = self._plan(root_meta={"structure_levels": []},
                          chip_meta={"structure_levels": [2100.0]})
        self.assertEqual(self._payload(plan).get("structure_levels"), [2100.0])
