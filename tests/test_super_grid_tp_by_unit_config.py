# -*- coding: utf-8 -*-
"""4.1 配置层验证：真实 super-grid.yaml 的 tp_by_unit 是否真能让档位表落盘。

与 test_levels_floor_tp_units 的分工：那个用**构造的 tpl** 测引擎逻辑；
这个用**真实 yaml 加载出来的 ladder_template** 测「配置有没有接上」——
yaml 缩进错位、键名拼错、被 overlay 覆盖等，只有走真实加载才暴露。

三项断言（对应 4.1 配置层修复的唯一意图）：
  1. 真实配置加载后 `ladder_template.tp_by_unit.enabled is True`
  2. 模板铺梯后 `data/bots/<bot>/state/ladder_levels.json` **存在且有档位**
  3. 空侧档位会被 `ensure_grid_tp` 留痕 `short_side_tp_not_implemented`
     （引擎现状：空侧尚未实现 —— 这条断言是**事实记录**，不是期望通过；
      实现空侧后应改为断言真实挂单）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402

MARK = 2332.71


class _Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class _Client:
    def __init__(self):
        self.book, self.prot = [], []

    def get_last_price(self, sym):
        return MARK

    def get_contract(self, sym):
        return _Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-sg"}
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


class _Report:
    def __init__(self):
        self.results = []


class TestSuperGridTpByUnitConfig(unittest.TestCase):
    """真实 super-grid.yaml 的 tp_by_unit 接线验证。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_bot_config(ROOT / "config" / "bots" / "super-grid.yaml")

    def _db(self) -> str:
        """最小 kline.db（引擎 auto_extract 兜底算结构位时读）。"""
        import sqlite3
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        con = sqlite3.connect(f.name)
        con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, "
                    "closed INT, o REAL, h REAL, l REAL, c REAL, atr14 REAL, "
                    "boll20 REAL, boll20_upper REAL, boll20_lower REAL)")
        for i in range(30):
            px = MARK * (1 - (0.3 + i * 0.4) / 100.0)
            con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("ETH_USDT", "15m", 1793170000 - i * 900, 1,
                         px, px + 5, px - 5, px, 7.5, MARK,
                         MARK * 1.013, MARK * 0.987))
        con.commit()
        con.close()
        return f.name

    def test_config_has_tp_by_unit_enabled(self):
        """① 真实 yaml 里 tp_by_unit.enabled 必须为 True（否则档位表永不写）。"""
        lt = self.cfg.ladder_template or {}
        tbp = lt.get("tp_by_unit") or {}
        self.assertTrue(tbp.get("enabled"),
                        f"tp_by_unit 未启用：{tbp!r} —— 档位表不会写，网格零逐档 TP")

    def test_config_keeps_martin_slots_and_20_lots(self):
        """② 顺带核对 4.0 张数结构没被 tp_by_unit 的插入破坏。"""
        lt = self.cfg.ladder_template or {}
        slots = lt.get("martin_slots") or {}
        self.assertEqual({int(k): v for k, v in slots.items()}, {1: 3, 5: 4, 10: 6})
        self.assertTrue(lt.get("martin_slots_by_index"))
        self.assertEqual(sum(slots.values()) + 7, 20)   # 3+4+6 + 7 网格 = 20

    def test_template_ladder_writes_levels_table(self):
        """③ 用真实配置跑一轮模板铺梯 → state/ladder_levels.json 必须落盘。"""
        # 真实 yaml 的 tpl，只把磁盘路径指向 tmp（其余 tp_by_unit/martin_slots 保持真实）
        lt = dict(self.cfg.ladder_template or {})
        lt["levels_state_file"] = str(self.tmp / "l.json")
        lt["anchor_state_file"] = str(self.tmp / "a.json")
        lt["levels_dynamic"] = dict(lt.get("levels_dynamic") or {})
        lt["levels_dynamic"]["db_path"] = self._db()
        lt["levels_dynamic"]["auto_extract"] = True

        ex = Executor(_Client(), symbols_whitelist=["ETH_USDT"],
                      root=self.tmp, bot_id="sgcfg")
        ex.ladder_template = lt
        ex._forced_side = "long"
        # 真实运行时 supervisor 会建好 bot state 目录；测试里手动建
        (self.tmp / "data" / "bots" / "sgcfg" / "state").mkdir(parents=True,
                                                               exist_ok=True)
        ex._round_meta = {
            "two_way": {"long": {"weight": 0.6, "levels": 10,
                                 "step_pct": 0.5, "lots": 1}},
            "structure_levels": [round(MARK * (1 - (0.3 + i * 0.4) / 100.0), 2)
                                 for i in range(20)],
            "ladder_step_pct": 0.5,
        }
        ex._complete_ladder([], _Report())

        path = (self.tmp / "data" / "bots" / "sgcfg" / "state" / "ladder_levels.json")
        self.assertTrue(path.exists(), "档位表未落盘 —— tp_by_unit 没接上")
        data = json.loads(path.read_text(encoding="utf-8"))
        levels = data.get("levels") or []
        self.assertGreaterEqual(len(levels), 8, f"档位数异常: {len(levels)}")
        # 每档必须有 tp_target（网格吃一格 / 马丁吃两格）
        for lv in levels:
            self.assertIsNotNone(lv.get("tp_target"),
                                 f"档位缺 tp_target: {lv}")
            self.assertEqual(lv.get("symbol"), "ETH_USDT")
        # 方向语义：多头 TP 在档价上方（低买高卖）、空头 TP 在档价下方
        by_side = {"long": 0, "short": 0}
        for lv in levels:
            side = str(lv.get("side") or "long")
            px, tp = float(lv["price"]), float(lv["tp_target"])
            by_side[side] = by_side.get(side, 0) + 1
            if side == "long":
                self.assertGreater(tp, px, f"多头 TP 应在档价上方: {lv}")
            else:
                self.assertLess(tp, px, f"空头 TP 应在档价下方: {lv}")
        # 真实配置是双向（two_way_sides: [long, short]）→ 两侧档位都应进表；
        # 空侧进表后仍会被 ensure_grid_tp 跳过（见 ④），但**表里必须有**
        self.assertGreater(by_side.get("short", 0), 0,
                           f"空侧档位未进表: {by_side}")

    def test_short_side_recorded_as_not_implemented(self):
        """④ 空侧现状留痕：ensure_grid_tp 只实现多侧（事实记录，非期望通过）。"""
        ex = Executor.__new__(Executor)
        ex.manage_positions = True
        ex.account_risk = {"grid_tp_auto": "live"}
        ex.bot_id = "sgcfg"
        ex.root = self.tmp
        path = self.tmp / "data" / "bots" / "sgcfg" / "state" / "ladder_levels.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"levels": [
            {"symbol": "ETH_USDT", "side": "short", "price": 2400.0,
             "lots": 1, "status": "pending", "tp_target": 2370.0},
        ]}), encoding="utf-8")
        ex._positions_or_error = lambda: ([], "")
        out = ex.ensure_grid_tp("ETH_USDT")
        skipped = out.get("skipped_side") or []
        self.assertTrue(skipped and skipped[0].get("why") == "short_side_tp_not_implemented",
                        f"空侧应留痕未实现，实际: {out}")


if __name__ == "__main__":
    unittest.main()
