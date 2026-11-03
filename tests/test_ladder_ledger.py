# -*- coding: utf-8 -*-
"""阶梯台账（全局系统层）与保护单张数对齐，2026-11-04。

锁三条真实病根（2026-11-03 实盘核实）：

1. **order_id 从不回写** → 台账与交易所无法对账
2. **成交档被每轮重建删掉** → 持仓的 tp_target 消失、止盈断源
3. **层数预算不计已成交** → 永远 10 层未成交，价格来回就无限重入

以及第 4 条：
4. **TP/SL 张数不随持仓联动** → TP 8 张 / SL 8 张 vs 持仓 5 张（超配）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha import ladder_ledger as ledger  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.reconcile import reconcile_exit_sizes  # noqa: E402

MARK = 2300.0


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    """最小交易所替身：book = 在盘限价单，prot = 条件单，positions = 持仓。"""

    def __init__(self):
        self.book = {}          # id -> {id, price, size, text, status}
        self.prot = []
        self.positions = []
        self.finished = {}      # id -> {"status": "finished", "finish_as": ...}
        self._seq = 0

    def get_last_price(self, sym):
        return MARK

    def get_ticker(self, sym):
        return {"mark_price": MARK, "last": MARK}

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return list(self.positions)

    def list_orders(self, sym=None):
        return [dict(o) for o in self.book.values()]

    def list_price_orders(self, sym=None):
        return list(self.prot)

    def place_order(self, body):
        self._seq += 1
        oid = "o%d" % self._seq
        self.book[oid] = {"id": oid, "price": str(body["price"]),
                          "size": body["size"], "text": body.get("text"),
                          "status": "open", "left": body["size"]}
        return {"id": oid}

    def place_price_order(self, body):
        self._seq += 1
        oid = "p%d" % self._seq
        self.prot.append({"id": oid, "status": "open", "initial": body["initial"],
                          "trigger": body["trigger"],
                          "create_time": float(self._seq)})
        return {"id": oid}

    def get_order(self, oid):
        if oid in self.book:
            return dict(self.book[oid])
        if oid in self.finished:
            return dict(self.finished[oid], id=oid)
        from omnialpha.gate_client import GateApiError
        raise GateApiError("404 not found", status=404)

    def cancel_order(self, oid):
        self.book.pop(oid, None)
        return {}

    def cancel_price_order(self, pid):
        self.prot = [p for p in self.prot if p.get("id") != pid]
        return {}

    # 测试辅助
    def fill(self, oid):
        self.book.pop(oid, None)
        self.finished[oid] = {"status": "finished", "finish_as": "filled"}


def _tpl(**over):
    t = {"grid_levels": 10, "tp_by_unit": {"enabled": True}, "base_position": {}}
    t.update(over)
    return t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.client = Client()
        self.ex = Executor(self.client, symbols_whitelist=["ETH_USDT"],
                           root=self.tmp, bot_id="led")
        self.ex.account_risk = {"grid_tp_auto": "live"}

    def path(self):
        return self.tmp / "data" / "bots" / "led" / "state" / "ladder_levels.json"

    def levels(self):
        if not self.path().exists():
            return []
        return json.loads(self.path().read_text(encoding="utf-8")).get("levels") or []

    def want(self, n=10, start=0.5, step=0.6, side="long"):
        out = []
        for i in range(n):
            px = MARK * (1 - (start + i * step) / 100.0) if side == "long" \
                else MARK * (1 + (start + i * step) / 100.0)
            out.append((round(px, 2), 1.0 if i not in (0, 4, 9) else (3.0, 4.0, 6.0)[(0, 4, 9).index(i)]))
        return out

    def sync(self, want, side="long"):
        # 持仓快照有 5s 进程内缓存（生产上一轮只读一次是优点），测试里毫秒级
        # 连续调用会拿到旧值 → 每次显式失效。
        self.ex._pos_cache = None
        units = []
        for i, _ in enumerate(want):
            units.append("martin" if i in (0, 4, 9) else "grid")
        return self.ex._sync_template_levels("ETH_USDT", side, want, units,
                                             _tpl(), 0.6)


class TestOrderIdBackfill(Base):
    """病根 1：order_id 必须回写，否则台账永远无法对账。"""

    def test_bind_by_price(self):
        w = self.want()
        self.sync(w)
        # 交易所侧挂出全部 10 档
        for px, lots in w:
            self.client.place_order({"contract": "ETH_USDT", "price": str(px),
                                     "size": lots, "text": "t-led"})
        self.sync(w)
        ids = [lv.get("order_id") for lv in self.levels()]
        self.assertEqual(len(ids), 10)
        self.assertTrue(all(ids), "回填后每档都该有 order_id")

    def test_placed_bind(self):
        w = self.want()
        self.sync(w)
        o = self.client.place_order({"contract": "ETH_USDT", "price": str(w[0][0]),
                                     "size": w[0][1], "text": "t-led"})
        self.ex._ladder_bind_placed("ETH_USDT", "long",
                                    [{"price": float(w[0][0]), "id": o["id"]}])
        rows = [lv for lv in self.levels() if abs(lv["price"] - w[0][0]) < 0.01]
        self.assertEqual(rows[0]["order_id"], o["id"], "铺单后必须回写单号")


class TestFilledKept(Base):
    """病根 2：成交档必须保留（它的 tp_target 是持仓止盈的唯一来源）。"""

    def test_filled_level_survives_resync(self):
        w = self.want()
        self.sync(w)
        placed = {}
        for px, lots in w:
            o = self.client.place_order({"contract": "ETH_USDT", "price": str(px),
                                         "size": lots, "text": "t-led"})
            placed[px] = o["id"]
        self.sync(w)                        # 回填单号
        # 第一档成交：从在盘移除、终态 filled、账户出现多头持仓
        first = w[0][0]
        self.client.fill(placed[first])
        self.client.positions = [{"contract": "ETH_USDT", "size": "3"}]
        self.sync(w)
        rows = [lv for lv in self.levels() if abs(lv["price"] - first) < 0.01]
        self.assertEqual(len(rows), 1, "成交档不能被删")
        self.assertEqual(rows[0].get("entry_state"), "filled")
        self.assertIsNotNone(rows[0].get("tp_target"), "成交档必须保留 tp_target")

    def test_cancelled_level_dropped(self):
        w = self.want()
        self.sync(w)
        o = self.client.place_order({"contract": "ETH_USDT", "price": str(w[0][0]),
                                     "size": w[0][1], "text": "t-led"})
        self.sync(w)
        self.client.book.pop(o["id"], None)          # 被撤（无终态记录 → gone）
        self.sync(w)
        rows = [lv for lv in self.levels() if abs(lv["price"] - w[0][0]) < 0.01
                and lv.get("entry_state") != "archived"]
        # 撤单档：reconcile 删掉后，本轮 want 里还有它 → 作为新档重新入表（pending）
        self.assertTrue(rows and rows[0]["status"] == "pending",
                        f"撤掉的档本轮应重新排成 pending：{rows}")


class TestBudget(Base):
    """病根 3：已成交档占层数额度 —— 成交 1 档 → 只剩 9 档可挂。"""

    def _seed_one_fill(self):
        w = self.want()
        self.sync(w)
        ids = {}
        for px, lots in w:
            ids[px] = self.client.place_order(
                {"contract": "ETH_USDT", "price": str(px), "size": lots,
                 "text": "t-led"})["id"]
        self.sync(w)
        self.client.fill(ids[w[0][0]])
        self.client.positions = [{"contract": "ETH_USDT", "size": "3"}]
        self.sync(w)
        return w

    def test_budget_drops_consumed_price(self):
        w = self._seed_one_fill()
        want2, units2, note = self.ex._apply_ladder_budget(
            "ETH_USDT", "long", w, ["martin"] + ["grid"] * 9, _tpl())
        self.assertEqual(note.get("filled"), 1, f"应识别 1 个已成交档：{note}")
        self.assertEqual(note.get("remaining"), 9)
        self.assertNotIn(w[0][0], [p for p, _ in want2],
                         "已成交价不得再出现在待挂列表（不能原地补）")
        self.assertEqual(len(want2), 9, f"剩余额度 9 → 只铺 9 档：{note}")
        self.assertEqual(len(units2), 9)

    def test_budget_full_after_flat(self):
        self._seed_one_fill()
        self.client.positions = []           # 平仓 → 整代归档
        want2, _, note = self.ex._apply_ladder_budget(
            "ETH_USDT", "long", self.want(), ["martin"] + ["grid"] * 9, _tpl())
        self.assertEqual(note.get("filled"), 0, f"无持仓 → 预算回满：{note}")
        self.assertEqual(len(want2), 10)

    def test_total_recorded_from_first_ladder(self):
        w = self.want(n=12)
        self.sync(w)
        data = ledger.read(self.path())
        self.assertEqual(ledger.get_total(data, "ETH_USDT", "long"), 12,
                         "总层数按首次铺出的层数定（AI 要 12 层就记 12）")


class TestExitSizeReconcile(unittest.TestCase):
    """病根 4：TP/SL 张数必须对齐最新持仓。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.client = Client()
        self.ex = Executor(self.client, symbols_whitelist=["ETH_USDT"],
                           root=self.tmp, bot_id="led", label_prefix="led")
        self.ex.account_risk = {"grid_tp_auto": "live"}

    def _tp(self, size):
        return self.client.place_price_order({
            "initial": {"contract": "ETH_USDT", "size": size, "price": "0",
                        "tif": "ioc", "reduce_only": True, "text": "t-led-tp"},
            "trigger": {"price": "2200", "rule": 2, "price_type": 1,
                        "expiration": 0}})["id"]

    def _sl(self, size):
        return self.client.place_price_order({
            "initial": {"contract": "ETH_USDT", "size": size, "price": "0",
                        "tif": "ioc", "reduce_only": True, "text": "t-led-sl"},
            "trigger": {"price": "1900", "rule": 2, "price_type": 1,
                        "expiration": 0}})["id"]

    def test_tp_trimmed_to_position(self):
        self.client.positions = [{"contract": "ETH_USDT", "size": "-5"}]
        self._tp(8)
        out = reconcile_exit_sizes(self.ex, "ETH_USDT")
        self.assertTrue(out["ok"], out)
        tp = [p for p in self.client.prot if (p["initial"].get("text") or "").endswith("-tp")]
        self.assertEqual(sum(abs(float(p["initial"]["size"])) for p in tp), 5,
                         f"TP 总张数必须等于持仓 5：{tp}")

    def test_sl_keeps_pending_cover(self):
        # 持仓 5 + 待成交 3 → SL 目标 8，不该被裁
        self.client.positions = [{"contract": "ETH_USDT", "size": "-5"}]
        self._sl(8)
        self.client.place_order({"contract": "ETH_USDT", "price": "2400",
                                 "size": -3, "text": "t-led"})
        out = reconcile_exit_sizes(self.ex, "ETH_USDT")
        self.assertTrue(out["ok"], out)
        self.assertFalse(out.get("sl"), f"SL 覆盖待成交档属正常，不该裁：{out}")
        sl = [p for p in self.client.prot if (p["initial"].get("text") or "").endswith("-sl")]
        self.assertEqual(sum(abs(float(p["initial"]["size"])) for p in sl), 8)

    def test_sl_trimmed_to_position_plus_pending(self):
        self.client.positions = [{"contract": "ETH_USDT", "size": "-2"}]
        self._sl(8)
        out = reconcile_exit_sizes(self.ex, "ETH_USDT")
        self.assertTrue(out["ok"], out)
        sl = [p for p in self.client.prot if (p["initial"].get("text") or "").endswith("-sl")]
        self.assertEqual(sum(abs(float(p["initial"]["size"])) for p in sl), 2,
                         f"无待成交时 SL 应裁到持仓 2：{sl}")

    def test_no_position_no_action(self):
        self._tp(3)
        out = reconcile_exit_sizes(self.ex, "ETH_USDT")
        self.assertTrue(out["ok"])
        self.assertIn("no_position", out["skipped"])


if __name__ == "__main__":
    unittest.main()


class TestReleaseOnArbitrage(Base):
    """用户口径（2026-11-04）：「网格套利一次那么就释放了一档网格」。

    两段语义必须同时成立，否则网格会跑歪：
    · 成交一档 → **占**额度（10 → 9，不原地补满）
    · 止盈落袋 → **释放**额度（9 → 10，该档重新开档）
    """

    def _seed_fill(self):
        w = self.want()
        self.sync(w)
        ids = {}
        for px, lots in w:
            ids[px] = self.client.place_order(
                {"contract": "ETH_USDT", "price": str(px), "size": lots,
                 "text": "t-led"})["id"]
        self.sync(w)
        self.client.fill(ids[w[0][0]])
        self.client.positions = [{"contract": "ETH_USDT", "size": "3"}]
        self.sync(w)
        return w

    def _close(self, price):
        p = self.path()
        d = json.loads(p.read_text(encoding="utf-8"))
        for lv in d["levels"]:
            if abs(lv["price"] - price) < 0.01:
                lv["status"] = "closed"
                lv.pop("tp_order_id", None)
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    def test_budget_released_after_tp(self):
        w = self._seed_fill()
        _, _, note = self.ex._apply_ladder_budget(
            "ETH_USDT", "long", w, ["martin"] + ["grid"] * 9, _tpl())
        self.assertEqual(note.get("filled"), 1, f"成交后应先占 1 档：{note}")
        self._close(w[0][0])                      # 止盈落袋
        want2, _, note2 = self.ex._apply_ladder_budget(
            "ETH_USDT", "long", w, ["martin"] + ["grid"] * 9, _tpl())
        self.assertEqual(note2.get("filled"), 0, f"套利完成后额度应释放：{note2}")
        self.assertEqual(note2.get("remaining"), 10)
        self.assertEqual(len(want2), 10, "释放后应回到 10 档")
        self.assertIn(w[0][0], [p for p, _ in want2],
                      "释放的档位应重新可挂（不是永久消失）")

    def test_released_level_reopens_as_pending(self):
        w = self._seed_fill()
        self._close(w[0][0])
        self.sync(w)                              # 下一轮：重开档
        rows = [lv for lv in self.levels() if abs(lv["price"] - w[0][0]) < 0.01]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending", f"应重开为 pending：{rows[0]}")
        self.assertEqual(rows[0].get("order_id"), "", "重开档要重新等交易所单号")
        self.assertIsNotNone(rows[0].get("tp_target"), "重开档仍要带 tp_target")
        self.assertGreaterEqual(int(rows[0].get("reopened") or 0), 1)


class TestSeedFromPosition(Base):
    """部署迁移：旧表 order_id 全空，用**持仓**反推已成交档（一次性）。

    不播种的后果：按「零消耗」起算 → 已成交档被当空闲额度重挂 → 敞口超过
    层数上限（10 层 + 已有持仓）。
    """

    def test_seed_marks_nearest_levels_filled(self):
        w = self.want()                    # 第 1 档 3 张，其余 1 张
        self.client.positions = [{"contract": "ETH_USDT", "size": "3"}]
        note = self.sync(w)
        rows = [lv for lv in self.levels() if lv.get("side") == "long"]
        filled = [x for x in rows if x.get("entry_state") == "filled"]
        self.assertEqual(len(filled), 1,
                         f"持仓 3 张 = 最近 1 档（3 张）已成交：{note}")
        self.assertAlmostEqual(filled[0]["price"], w[0][0], places=2)
        self.assertIn("seeded", note)

    def test_seed_counts_against_budget(self):
        w = self.want()
        self.client.positions = [{"contract": "ETH_USDT", "size": "3"}]
        self.sync(w)
        want2, _, note = self.ex._apply_ladder_budget(
            "ETH_USDT", "long", w, ["martin"] + ["grid"] * 9, _tpl())
        self.assertEqual(note.get("filled"), 1, f"播种后应占 1 档：{note}")
        self.assertEqual(len(want2), 9, "剩 9 档可挂（不能把已成交档重挂）")

    def test_seed_runs_once(self):
        w = self.want()
        self.client.positions = [{"contract": "ETH_USDT", "size": "3"}]
        self.sync(w)
        n1 = len(self.levels())
        self.client.positions = []
        self.sync(w)                       # 第二次不再播种（不重复加行）
        self.assertLessEqual(len(self.levels()), n1 + 10)
        data = ledger.read(self.path())
        self.assertTrue(ledger.is_seeded(data, "ETH_USDT", "long"))
