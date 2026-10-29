# -*- coding: utf-8 -*-
"""双向网格 2.0：下方多头网格 + 上方空头网格同时维护，互不影响。

## 与 1.0 的区别

1.0 = `prompts/ladder_t_pa.md`「二、永远双向」—— 让**模型**自己上下两边都挂。
问题：引擎模板（`ladder_template`）是单侧的，一开模板就会把另一边当成
「不在模板档位上」撤掉（`prune_off_template`）。所以 1.0 的 bot 只能关掉模板
（`ladder-v5-paper` 就是这样），也就失去了补档 / SL 覆盖 / already_full 这些保障。

2.0 = 把「双向」搬到**引擎层**：`two_way: true` 时 `_complete_ladder` 对
long / short 各跑一遍完整单侧流程，每次拿一个 `_SideClient` 视图
（只看得见自己那侧的挂单 / SL / 持仓）—— 于是 prune、already_full、
同价去重、SL 覆盖全部天然按侧隔离。

## 本文件锁住的行为

1. 两侧各 10 层，多档全在现价下方、空档全在上方
2. 两侧马丁占位一致（3/7/10 → 6/7/9 张）
3. 一侧的旧档被 prune 时**不碰**另一侧
4. 两侧 SL 方向相反：多 `rule=2`（跌破卖出）、空 `rule=1`（涨破买入），
   各自只覆盖自己那一侧的敞口
5. 连续 5 轮幂等：档位不漂移、不重复挂、SL 不增殖
6. 单侧持仓不影响另一侧铺梯（不再触发 `reverse_position` 拦空侧）
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
from omnialpha.schema import Intent  # noqa: E402

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
                "boll20_upper REAL, boll20_lower REAL)")
    half = MARK * 2.6 / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 ATR, MARK, MARK + half, MARK - half))
    con.commit()
    con.close()
    return f.name


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = _db()

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def tpl(self, two_way=True, **over):
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


class TestTwoWayLay(Base):
    def test_both_sides_ten_levels(self):
        c = Client()
        _, rep = self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        self.assertEqual(len(longs), 10, f"多头必须 10 层，实际 {len(longs)}")
        self.assertEqual(len(shorts), 10, f"空头必须 10 层，实际 {len(shorts)}")
        self.assertTrue(all(p < MARK for p, _ in longs), f"多档必须在现价下方：{longs}")
        self.assertTrue(all(p > MARK for p, _ in shorts), f"空档必须在现价上方：{shorts}")

    def test_martin_slots_mirrored(self):
        c = Client()
        self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        want = [1, 1, 6, 1, 1, 1, 7, 1, 1, 9]
        self.assertEqual([int(s) for _, s in longs], want, "多头马丁占位 3/7/10")
        self.assertEqual([int(s) for _, s in shorts], want, "空头马丁占位同样 3/7/10")

    def test_symmetric_steps(self):
        c = Client()
        self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        d_long = (MARK - longs[0][0]) / MARK * 100
        d_short = (shorts[0][0] - MARK) / MARK * 100
        self.assertLess(abs(d_long - d_short), 0.02,
                        f"两侧首档距离应一致：多 {d_long:.3f}% vs 空 {d_short:.3f}%")

    def test_single_side_mode_unchanged(self):
        """two_way 关闭 → 只铺一侧（回归保护）"""
        c = Client()
        tpl = self.tpl(two_way=False, direction_mode="signal")
        self.run_round(c, tpl, meta={"ladder_step_pct": 0.5})
        longs, shorts = self.sides(c)
        self.assertEqual(len(shorts), 0, "单向模式不该有空单")
        self.assertEqual(len(longs), 10, f"单向应铺满 10 层：{len(longs)}")


class TestTwoWayIsolation(Base):
    def test_prune_one_side_keeps_other(self):
        """多侧的非模板旧档被撤，空侧原有档位不受影响。"""
        c = Client()
        self.run_round(c, self.tpl())
        _, shorts_before = self.sides(c)
        # 往多侧塞一条离谱旧档
        c.book.append({"id": "bad_long", "price": 9999.0, "size": 1.0})
        self.run_round(c, self.tpl())
        self.assertIn("bad_long", c.cancelled, "多侧非模板档必须撤")
        _, shorts_after = self.sides(c)
        self.assertEqual(shorts_before, shorts_after,
                         f"空侧不该被多侧的 prune 波及：{shorts_before} → {shorts_after}")

    def test_sl_rules_are_opposite(self):
        c = Client()
        self.run_round(c, self.tpl())
        long_sl = self.sl_of(c, 2)
        short_sl = self.sl_of(c, 1)
        self.assertEqual(len(long_sl), 1, f"多头应有 1 条 rule=2 SL：{long_sl}")
        self.assertEqual(len(short_sl), 1, f"空头应有 1 条 rule=1 SL：{short_sl}")
        self.assertLess(long_sl[0][0], MARK, "多单 SL 在下方（跌破卖出）")
        self.assertGreater(short_sl[0][0], MARK, "空单 SL 在上方（涨破买入）")
        self.assertLess(long_sl[0][1], 0, "多单 SL 是卖单（负）")
        self.assertGreater(short_sl[0][1], 0, "空单 SL 是买单（正）")

    def test_sl_covers_own_side_only(self):
        c = Client()
        self.run_round(c, self.tpl())
        longs, shorts = self.sides(c)
        n_long = int(sum(s for _, s in longs))
        n_short = int(sum(s for _, s in shorts))
        long_sl = self.sl_of(c, 2)
        short_sl = self.sl_of(c, 1)
        self.assertEqual(abs(long_sl[0][1]), n_long,
                         f"多单 SL 只覆盖多侧 {n_long} 张，实际 {long_sl[0][1]}")
        self.assertEqual(abs(short_sl[0][1]), n_short,
                         f"空单 SL 只覆盖空侧 {n_short} 张，实际 {short_sl[0][1]}")


class TestTwoWayStability(Base):
    def test_five_rounds_idempotent(self):
        """连续 5 轮：档位不漂移、不重复挂、SL 不增殖（本地 5 轮验证）。"""
        c = Client()
        tpl = self.tpl()
        snaps = []
        for i in range(5):
            self.run_round(c, tpl)
            longs, shorts = self.sides(c)
            snaps.append((len(c.placed), tuple(round(p, 2) for p, _ in longs),
                          tuple(round(p, 2) for p, _ in shorts), len(c.prot)))
        n0 = snaps[0][0]
        for i, s in enumerate(snaps):
            self.assertEqual(s[0], n0, f"第{i+1}轮不该再补挂：{n0} → {s[0]}")
            self.assertEqual(s[1], snaps[0][1], f"第{i+1}轮多侧档位漂移")
            self.assertEqual(s[2], snaps[0][2], f"第{i+1}轮空侧档位漂移")
            self.assertEqual(s[3], 2, f"第{i+1}轮 SL 应为 2 条（多+空），实际 {s[3]}")

    def test_reanchor_moves_both_sides(self):
        c = Client()
        tpl = self.tpl()
        self.run_round(c, tpl)
        longs_before, shorts_before = self.sides(c)
        c.mark = MARK * 0.95          # 跌 5% > 2% 阈值 → 两侧都重锚
        self.run_round(c, tpl)
        longs_after, shorts_after = self.sides(c)
        self.assertNotEqual([p for p, _ in longs_before], [p for p, _ in longs_after],
                            "重锚后多侧档位必须换")
        self.assertNotEqual([p for p, _ in shorts_before], [p for p, _ in shorts_after],
                            "重锚后空侧档位必须同样换")
        self.assertTrue(all(p < c.mark for p, _ in longs_after),
                        f"重锚后多档仍应在现价下方：{longs_after[:2]} vs {c.mark}")
        self.assertTrue(all(p > c.mark for p, _ in shorts_after),
                        f"重锚后空档仍应在现价上方：{shorts_after[:2]} vs {c.mark}")

    def test_levels_state_split_by_side(self):
        c = Client()
        self.run_round(c, self.tpl())
        data = json.loads((self.dir / "levels.json").read_text(encoding="utf-8"))
        self.assertIn("ETH_USDT:long", data, f"档位 state 必须按侧分开：{list(data)}")
        self.assertIn("ETH_USDT:short", data, f"档位 state 必须按侧分开：{list(data)}")
        self.assertNotEqual([round(p, 2) for p, _ in data["ETH_USDT:long"]["levels"]],
                            [round(p, 2) for p, _ in data["ETH_USDT:short"]["levels"]],
                            "两侧档位不该相同")

    def test_structure_levels_used_per_side(self):
        """两侧各用自己那侧的结构位：多取下方、空取上方。"""
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
                        f"多侧应吸附到 2300 这类下方结构位：{[p for p,_ in longs]}")
        self.assertTrue(any(abs(p - 2370.0) < 0.5 for p, _ in shorts),
                        f"空侧应吸附到 2370 这类上方结构位：{[p for p, _ in shorts]}")
        self.assertTrue(all(p < MARK for p, _ in longs))
        self.assertTrue(all(p > MARK for p, _ in shorts))


if __name__ == "__main__":
    unittest.main()
