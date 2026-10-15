# -*- coding: utf-8 -*-
"""假网格修复（2026-10-15）回归测试：引擎分治 + 档位表 + 人格静态锁。

**背景（martin-grid 产线实测）**：整仓共用 1 个 TP（2499），网格级套利从未
发生 —— 「假网格」。因果链三层：
  ① 人格 §3.3「逐档止盈对账」从未执行（无输入：持仓快照没有分笔成交价）；
  ② 引擎 `_modify_tp_sl`：一腿触发价非法 → **整笔拒**（网格档中值在现价下方
     → 所有档位永远改不了，LLM 连续 10+ 轮只能 hold）；撤旧 TP 时**全撤**；
     share 缺省 = 单腿**全平**；
  ③ 信息层：account 快照无每档买入价/张数/目标止盈。

本文件锁死引擎/信息层修复（人格层改写另有 §十 静态检查兜底）：
  E1  `_filter_illegal_exit_legs` + `_modify_tp_sl` **分治**：非法腿跳过 +
       `detail["skipped_illegal"]` 留痕，合法腿照挂、旧结构照撤
  E1c 入口路径 `_precheck_exit_triggers` 仍是「一腿非法整笔中止」（回归护栏：
       未成交挂单可以宁可不交易，两条路径语义不许互相污染）
  E2  `TP_STRUCT_COLLAPSE`：多腿 TP 结构被单腿替换 → WARNING + detail 留痕
  P1  `_record_ladder_level`：档位表落盘（原子写 / 有界 / 失败不拦主流程）
  P2  account 工具注入 `ladder_levels`（LLM 逐档对账的输入）

⚠️ Gate 硬规则（长仓 TP rule=1 要求触发价 > mark）：网格档中值/结构位在现价
   **下方**时交易所直接拒 —— modify 路径的「非法腿跳过」是唯一可行出口，
   这就是 E1 分治的存在理由；入口路径不能照抄（未成交挂单 + 无保护 = 裸仓）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta  # noqa: E402
from omnialpha.schema import Intent, infer_trigger_rules  # noqa: E402
from omnialpha.strategist.tools import run_tool  # noqa: E402

META = ContractMeta(name="ETH_USDT", quanto_multiplier=0.01,
                    order_size_round=1, order_price_round=0.05, leverage_max=50)

MARK = 2400.0   # 参考价：网格档中值（2362 一类）都在它下方 → 非法腿的来源


class FakeClient:
    """够 modify_tp_sl / account 工具用的最小 Gate 客户端桩。"""

    def __init__(self, positions=None, price_orders=None, open_orders=None,
                 mark=MARK, account=None):
        self._positions = list(positions or [])
        self._price_orders = list(price_orders or [])
        self._open_orders = list(open_orders or [])
        self._mark = mark
        self._account = account or {"available": "100", "total": "200"}
        self.placed = []       # place_price_order 收到的 body
        self.cancelled = []    # cancel_price_order 收到的 id
        self.ticker_calls = 0

    def get_positions(self):
        return list(self._positions)

    def list_price_orders(self, symbol):
        return list(self._price_orders)

    def list_orders(self, symbol=None):
        return list(self._open_orders)

    def get_contract(self, symbol):
        return META

    def get_last_price(self, symbol):
        return 2300.0

    def get_ticker(self, symbol):
        self.ticker_calls += 1
        return {"mark_price": str(self._mark), "last": str(self._mark)}

    def get_price_order(self, oid):
        # 落盘确认：挂上的条件单都算「活着」（cancelled 由撤单测试另行构造）
        return {"id": str(oid), "status": "open"}

    def get_account(self):
        return dict(self._account)

    def place_price_order(self, body):
        self.placed.append(body)
        return {"id": "new%d" % len(self.placed)}

    def cancel_price_order(self, pid):
        self.cancelled.append(str(pid))
        return {"id": pid}


def _pos(size=6, entry=2380.0, mark=0.0):
    """多头持仓；mark=0 模拟「持仓记录里没有 mark_price」的老响应。"""
    return {"contract": "ETH_USDT", "size": size, "entry_price": str(entry),
            "mark_price": str(mark), "mode": "single_way"}


def _tp(pid, price, size, text="t-mtg-tp", status="open"):
    return {"id": str(pid),
            "initial": {"text": text, "trigger_price": str(price),
                        "size": size, "reduce_only": True},
            "trigger": {"price": str(price), "rule": 1, "price_type": 0},
            "status": status}


def _sl(pid, price, size, text="t-mtg-sl", status="open"):
    return {"id": str(pid),
            "initial": {"text": text, "trigger_price": str(price),
                        "size": size, "reduce_only": True},
            "trigger": {"price": str(price), "rule": 2, "price_type": 0},
            "status": status}


def _modify_exec(cli, positions=None):
    td = tempfile.TemporaryDirectory()
    # 由调用方负责 cleanup（本文件的 TestCase 统一 addCleanup）
    ex = Executor(cli, label_prefix="mtg", root=Path(td.name),
                  bot_id="martin-grid")
    return ex, td


def _tp_prices(cli):
    return sorted(float(b["trigger"]["price"]) for b in cli.placed
                  if b["initial"]["text"].endswith("-tp"))


# ───────────────────────── E1：modify 路径非法腿分治 ─────────────────────────

class TestFilterIllegalExitLegs(unittest.TestCase):
    """`_filter_illegal_exit_legs` / `_rebuild_tp_levels` 单元契约。"""

    def _ex(self, mark=MARK):
        cli = FakeClient(mark=mark)
        ex, td = _modify_exec(cli)
        self.addCleanup(td.cleanup)
        return ex, cli

    def test_split_legal_and_illegal(self):
        """mark=2400：tp 2440 合法、tp 2380 非法、sl 2350 合法（rule=2 需 < mark）。"""
        ex, _ = self._ex()
        legs = [("tp", 2440.0, 1), ("tp", 2380.0, 1), ("sl", 2350.0, 2)]
        good, bad = ex._filter_illegal_exit_legs(
            Intent(action="modify_tp_sl", symbol="ETH_USDT", side="long"), legs)
        self.assertEqual([("tp", 2440.0, 1), ("sl", 2350.0, 2)], good)
        self.assertEqual(1, len(bad))
        self.assertIn("2380", bad[0])

    def test_no_ref_price_keeps_all(self):
        """取不到参考价（ticker 抛错）→ 不拦，全部放行（与 precheck 同口径）。"""
        cli = FakeClient(mark=MARK)

        def _boom(sym):
            raise RuntimeError("ticker down")

        cli.get_ticker = _boom
        ex, td = _modify_exec(cli)
        self.addCleanup(td.cleanup)
        legs = [("tp", 2380.0, 1)]
        good, bad = ex._filter_illegal_exit_legs(
            Intent(action="modify_tp_sl", symbol="ETH_USDT", side="long"), legs)
        self.assertEqual(legs, good)
        self.assertEqual([], bad)

    def test_empty_legs(self):
        ex, _ = self._ex()
        good, bad = ex._filter_illegal_exit_legs(
            Intent(action="modify_tp_sl", symbol="ETH_USDT"), [])
        self.assertEqual([], good)
        self.assertEqual([], bad)

    def test_rebuild_collapses_tp_fields(self):
        """串档回归：过滤后必须整体重建 —— 只改 tp 会把 tp2/tp3 留下。"""
        it = Intent(action="modify_tp_sl", symbol="ETH_USDT", tp=2440.0,
                    tp2=2380.0, tp3=2460.0, tp1_share=0.5, tp2_share=0.25,
                    tp3_share=0.25, tp_extra=[2480.0], tp_extra_shares=[0.1])
        Executor._rebuild_tp_levels(it, [(2440.0, 0.5), (2460.0, 0.25)])
        self.assertEqual(2440.0, it.tp)
        self.assertEqual(2460.0, it.tp2)
        self.assertIsNone(it.tp3)
        self.assertEqual(0.5, it.tp1_share)
        self.assertEqual(0.25, it.tp2_share)
        self.assertIsNone(it.tp3_share)
        self.assertEqual([], it.tp_extra)
        self.assertEqual([], it.tp_extra_shares)


class TestModifySkipsIllegalLegs(unittest.TestCase):
    """`_modify_tp_sl` 分治集成：非法腿跳过留痕，合法腿照挂。"""

    def _run(self, intent, price_orders=None, positions=None):
        if positions is None:
            positions = [_pos(6)]      # 默认 6 张多头 @2380（mark 由 ticker 给）
        cli = FakeClient(positions=positions, price_orders=price_orders)
        ex, td = _modify_exec(cli)
        self.addCleanup(td.cleanup)
        return ex._modify_tp_sl(intent), cli

    def _intent(self, **kw):
        it = Intent(action="modify_tp_sl", symbol="ETH_USDT", side="long",
                    label="mtg", **kw)
        it.trigger_rule_tp = infer_trigger_rules("open_long", True)
        it.trigger_rule_sl = infer_trigger_rules("open_long", False)
        return it

    def test_one_illegal_of_three_others_placed(self):
        """实盘原型：3 档 TP 里 1 档中值在现价下方 → 另 2 档照挂、1 档留痕。"""
        old = [_tp("o1", 2420.0, -6), _tp("o2", 2440.0, -6), _tp("o3", 2460.0, -6)]
        it = self._intent(tp=2440.0, tp2=2380.0, tp3=2460.0,
                          tp1_share=1 / 3, tp2_share=1 / 3, tp3_share=1 / 3)
        res, cli = self._run(it, price_orders=old)
        self.assertTrue(res.ok, res.error)
        # 合法 2 档挂上，非法 2380 没挂。张数：share 各 1/3（和为 2/3 < 1）时
        # 末腿吃差 → [2, 4]（引擎既定语义：合计恒 = 持仓、绝不超配）
        self.assertEqual([2440.0, 2460.0], _tp_prices(cli))
        sizes = sorted(-b["initial"]["size"] for b in cli.placed
                       if b["initial"]["text"].endswith("-tp"))
        self.assertEqual([2, 4], sizes)
        self.assertEqual(6, sum(sizes))
        # 旧 3 条 TP 全撤（换新结构），非法腿留痕
        self.assertEqual(sorted(["o1", "o2", "o3"]), sorted(cli.cancelled))
        self.assertEqual(1, len(res.detail["skipped_illegal"]))
        self.assertIn("2380", res.detail["skipped_illegal"][0])
        # 重建后 tp3 串档字段被清掉（只留 2 档）
        self.assertIsNone(it.tp3)

    def test_all_illegal_is_benign_noop_keeps_old(self):
        """全部非法 → 良性 no-op（成功、不挂不撤、旧结构原样保留）。"""
        old = [_tp("o1", 2380.0, -6), _tp("o2", 2360.0, -6)]
        it = self._intent(tp=2380.0, tp2=2360.0, tp1_share=0.5, tp2_share=0.5)
        res, cli = self._run(it, price_orders=old)
        self.assertTrue(res.ok, res.error)
        self.assertEqual([], cli.placed)
        self.assertEqual([], cli.cancelled)          # 旧结构不被撤
        self.assertEqual("all exit legs illegal; old structure kept",
                         res.detail.get("noop"))
        self.assertEqual(2, len(res.detail["skipped_illegal"]))

    def test_sl_illegal_tp_still_modified(self):
        """SL 触发价非法（持仓记录无 mark → 纪律放行；ticker mark 拦下）→
        SL 腿跳过、TP 照改 —— 保护「TP 能改」不被一条坏 SL 拖死。"""
        it = self._intent(tp=2499.0, sl=2450.0)      # sl 2450 > mark 2400 → 非法
        res, cli = self._run(it, positions=[_pos(mark=0.0)],
                             price_orders=[_sl("s1", 2350.0, -6)])
        self.assertTrue(res.ok, res.error)
        self.assertEqual([2499.0], _tp_prices(cli))   # TP 照挂
        self.assertEqual(1, len(res.detail["skipped_illegal"]))
        self.assertIn("sl=2450", res.detail["skipped_illegal"][0])
        self.assertEqual([], cli.cancelled)            # SL 腿被丢 → 旧 SL 原样保留

    def test_all_legal_unchanged_behavior(self):
        """回归：全部合法时行为不变（3 档照挂、旧的全撤、无 skipped 留痕）。"""
        old = [_tp("o1", 2420.0, -6)]
        it = self._intent(tp=2440.0, tp2=2460.0, tp3=2480.0,
                          tp1_share=1 / 3, tp2_share=1 / 3, tp3_share=1 / 3)
        res, cli = self._run(it, price_orders=old)
        self.assertTrue(res.ok, res.error)
        self.assertEqual([2440.0, 2460.0, 2480.0], _tp_prices(cli))
        self.assertNotIn("skipped_illegal", res.detail)
        self.assertEqual(["o1"], cli.cancelled)


# ───────────────── E1c：入口路径仍是「一腿非法整笔中止」 ─────────────────

class TestOpenPathStillWholeBatchReject(unittest.TestCase):
    """`_precheck_exit_triggers`（_open/_stop_entry 专用）不许被分治污染。"""

    def _ex(self, mark=MARK):
        cli = FakeClient(mark=mark)
        ex, td = _modify_exec(cli)
        self.addCleanup(td.cleanup)
        return ex

    def test_one_illegal_leg_rejects_whole_batch(self):
        """入场腿 tp=2440 合法 + tp2=2380 非法 → 整笔拒（连入场单也不下）。"""
        it = Intent(action="open_long", symbol="ETH_USDT", side="long",
                    price=2380.0, sl=2350.0, tp=2440.0, tp2=2380.0)
        it.trigger_rule_tp = infer_trigger_rules("open_long", True)
        it.trigger_rule_sl = infer_trigger_rules("open_long", False)
        err = self._ex()._precheck_exit_triggers(it)
        self.assertIsNotNone(err)
        self.assertTrue(err.startswith("TRIGGER_PRICE_SIDE"), err)
        self.assertIn("2380", err)

    def test_all_legal_passes(self):
        it = Intent(action="open_long", symbol="ETH_USDT", side="long",
                    price=2380.0, sl=2350.0, tp=2440.0, tp2=2460.0)
        it.trigger_rule_tp = infer_trigger_rules("open_long", True)
        it.trigger_rule_sl = infer_trigger_rules("open_long", False)
        self.assertIsNone(self._ex()._precheck_exit_triggers(it))

    def test_no_ref_price_does_not_block(self):
        cli = FakeClient(mark=MARK)

        def _boom(sym):
            raise RuntimeError("ticker down")

        cli.get_ticker = _boom
        ex, td = _modify_exec(cli)
        self.addCleanup(td.cleanup)
        it = Intent(action="open_long", symbol="ETH_USDT", side="long",
                    price=2380.0, sl=2350.0, tp=2440.0, tp2=2380.0)
        it.trigger_rule_tp = infer_trigger_rules("open_long", True)
        self.assertIsNone(ex._precheck_exit_triggers(it))


# ───────────────────── E2：TP 结构塌方告警 ─────────────────────

class TestTpStructCollapse(unittest.TestCase):
    """多腿 TP 被单腿替换 → WARNING + `detail["tp_struct_collapse"]`。"""

    def _run(self, intent, price_orders=None):
        cli = FakeClient(positions=[_pos(6)], price_orders=price_orders)
        ex, td = _modify_exec(cli)
        self.addCleanup(td.cleanup)
        return ex._modify_tp_sl(intent), cli

    def _intent(self, **kw):
        it = Intent(action="modify_tp_sl", symbol="ETH_USDT", side="long",
                    label="mtg", **kw)
        it.trigger_rule_tp = infer_trigger_rules("open_long", True)
        return it

    def test_collapse_warns_and_leaves_trace(self):
        """假网格事故的引擎侧形态：旧 3 腿 TP → 新 1 个无 share 单 tp 覆盖全仓。"""
        old = [_tp("o1", 2420.0, -2), _tp("o2", 2440.0, -2), _tp("o3", 2460.0, -2)]
        it = self._intent(tp=2499.0)      # 无 share → 单腿全平（6 张）
        with self.assertLogs("omnialpha.executor", level="WARNING") as cm:
            res, cli = self._run(it, price_orders=old)
        self.assertTrue(res.ok, res.error)
        self.assertEqual([2499.0], _tp_prices(cli))
        self.assertEqual(6, -cli.placed[0]["initial"]["size"])   # 覆盖全仓
        self.assertEqual(sorted(["o1", "o2", "o3"]), sorted(cli.cancelled))
        self.assertTrue(any("TP_STRUCT_COLLAPSE" in m for m in cm.output))
        self.assertEqual({"old_legs": 3, "new_legs": 1},
                         res.detail["tp_struct_collapse"])

    def test_multi_to_multi_no_warning(self):
        """3 腿 → 3 腿（全部合法）：结构未塌，不留痕、不告警。"""
        old = [_tp("o1", 2420.0, -2), _tp("o2", 2440.0, -2), _tp("o3", 2460.0, -2)]
        it = self._intent(tp=2440.0, tp2=2460.0, tp3=2480.0,
                          tp1_share=1 / 3, tp2_share=1 / 3, tp3_share=1 / 3)
        res, cli = self._run(it, price_orders=old)
        self.assertTrue(res.ok, res.error)
        self.assertNotIn("tp_struct_collapse", res.detail)
        self.assertEqual([2440.0, 2460.0, 2480.0], _tp_prices(cli))

    def test_cancelled_old_tp_not_counted(self):
        """已 cancelled 的旧 TP 不参与「旧腿数」统计（不虚报塌方）。"""
        old = [_tp("o1", 2420.0, -2, status="cancelled")]
        it = self._intent(tp=2499.0)
        res, cli = self._run(it, price_orders=old)
        self.assertTrue(res.ok, res.error)
        self.assertNotIn("tp_struct_collapse", res.detail)
        self.assertEqual([], cli.cancelled)      # 终态单不重复撤


# ───────────── E3：TP 腿组对齐（_align_tp_group / _apportion_sizes）─────────────

class TestAlignTpGroup(unittest.TestCase):
    """减仓后多级 TP 按「组」对齐：合计恒=持仓、逐档结构保持、先挂后撤。

    事故形态：`_resync_protectors` 旧逻辑逐腿 `|size| != 持仓 → 放大到全仓`
    —— 逐档 TP 2/2/2、持仓 4 → 4/4/4 = 12 张（3 倍超配），且先撤后挂中间
    还有保护空窗。触发路径：reduce_long 落袋 → `_close` → 本方法。
    """

    def _run(self, position, price_orders):
        cli = FakeClient(positions=[_pos(position)], price_orders=price_orders)
        ex, td = _modify_exec(cli)
        self.addCleanup(td.cleanup)
        return ex._resync_protectors("ETH_USDT"), cli

    def _tp_placed(self, cli):
        """按 trigger 价排序的 (trigger, size) —— size 取负号前的绝对值。"""
        return sorted((float(b["trigger"]["price"]),
                       -int(b["initial"]["size"])) for b in cli.placed
                      if b["initial"]["text"].endswith("-tp"))

    def test_group_sum_equals_position_is_noop(self):
        """稳态：合计 == 持仓 → 一动不动（零 churn，逐档结构保持）。"""
        notes, cli = self._run(6, [_tp("a", 2600.0, -2), _tp("b", 2650.0, -2),
                                   _tp("c", 2700.0, -2)])
        self.assertEqual([], [n for n in notes if "resized" in n])
        self.assertEqual([], cli.placed)
        self.assertEqual([], cli.cancelled)

    def test_reduce_then_group_realigned(self):
        """reduce_long 后持仓 4、旧档合计 6 → 最大余数法分 4 张。"""
        notes, cli = self._run(4, [_tp("a", 2600.0, -2), _tp("b", 2650.0, -2),
                                   _tp("c", 2700.0, -2)])
        # 权重 2:2:2 分 4 → floor [1,1,1] 余 1 → 并列余数按序补第一条 → [2,1,1]
        # a 尺寸正好（2→2）→ 旧单即新单不重挂；b/c 各 2→1 撤旧挂新
        self.assertEqual([(2650.0, 1), (2700.0, 1)], self._tp_placed(cli))
        self.assertEqual(sorted(["b", "c"]), sorted(cli.cancelled))
        self.assertEqual(2, len([n for n in notes if "resized" in n]))
        # 覆盖合计：新挂 1+1 + 保留的 a(2) = 4 == 持仓
        self.assertEqual(4, sum(s for _, s in self._tp_placed(cli)) + 2,
                         "合计恒等于持仓（绝不超配）")
        # 结构保持：三条触发价都还在（a 未动、b/c 换单不换价）
        self.assertEqual([2600.0, 2650.0, 2700.0],
                         sorted({float(b["trigger"]["price"]) for b in cli.placed}
                                | {2600.0}))

    def test_unchanged_size_not_replaced(self):
        """组内有腿分摊后尺寸不变 → 旧单即新单，不重挂不撤（保结构 + 零 churn）。"""
        notes, cli = self._run(5, [_tp("a", 2600.0, -2), _tp("b", 2650.0, -2),
                                   _tp("c", 2700.0, -2)])
        # 2:2:2 分 5 → floor [1,1,1] 余 2 → 并列余数按序补前两条 → [2,2,1]
        self.assertEqual([(2700.0, 1)], self._tp_placed(cli))
        self.assertEqual(["c"], cli.cancelled)   # 只有 c 变了才撤
        self.assertEqual(1, len([n for n in notes if "resized" in n]))

    def test_zero_size_leg_cancelled(self):
        """持仓不够分：0 张腿 → 只撤不挂；其余腿保大弃小。"""
        notes, cli = self._run(1, [_tp("a", 2600.0, -5), _tp("b", 2650.0, -5)])
        # 5:5 分 1 → [1, 0]（并列余数按序）
        self.assertEqual([(2600.0, 1)], self._tp_placed(cli))
        self.assertEqual(sorted(["a", "b"]), sorted(cli.cancelled))
        self.assertEqual(1, sum(s for _, s in self._tp_placed(cli)))

    def test_place_failure_keeps_old(self):
        """挂新失败 → 旧单原样保留（保护不退化），且不留『已调整』假象。"""
        cli = FakeClient(positions=[_pos(10)], price_orders=[_tp("a", 2600.0, -6)])
        ex, td = _modify_exec(cli)
        self.addCleanup(td.cleanup)
        orig_place = cli.place_price_order
        def _boom(body):
            raise RuntimeError("exchange down")
        cli.place_price_order = _boom
        self.addCleanup(setattr, cli, "place_price_order", orig_place)
        notes = ex._resync_protectors("ETH_USDT")
        self.assertEqual([], cli.placed)
        self.assertEqual([], cli.cancelled, "挂不上绝不撤旧的（宁可不调也不裸奔）")
        self.assertTrue(any("resize_error" in n for n in notes), notes)

    def test_mixed_sl_merge_and_tp_noop(self):
        """SL 归并与 TP 组对齐互不干扰：SL 并成一条全量、TP 稳态不动。"""
        notes, cli = self._run(4, [_sl("s1", 2325.0, -2), _sl("s2", 2325.0, -2),
                                   _tp("a", 2600.0, -2), _tp("b", 2650.0, -2)])
        merged = [n for n in notes if "merged_sl" in n]
        self.assertEqual(1, len(merged))
        self.assertEqual(4, merged[0]["to_size"])
        tp_notes = [n for n in notes if "resized" in n]
        self.assertEqual([], tp_notes, "TP 合计 4 == 持仓 4 → 不动")
        self.assertEqual([], [b for b in cli.placed
                              if b["initial"]["text"].endswith("-tp")])
        self.assertEqual(sorted(["s1", "s2"]), sorted(cli.cancelled))

    def test_single_leg_still_resized(self):
        """单腿 TP（brooks 类旧形态）：-6 @ 持仓 10 → 撤旧挂新 -10（行为不变）。"""
        notes, cli = self._run(10, [_tp("a", 2600.0, -6)])
        self.assertEqual([(2600.0, 10)], self._tp_placed(cli))
        self.assertEqual(["a"], cli.cancelled)

    def test_full_coverage_leg_not_redistributed(self):
        """马丁共用 TP 覆盖全仓 + 逐档 TP 各自部分 → **不重分摊**。

        组合计 7×1 + 29 = 36 > 持仓 28（有意重叠）。旧行为会把全仓腿按
        `total×total/组合计` 挤到 23、逐档腿挤到 0（撤单）→ 出口结构被悄悄改小。
        正确行为：只把 29 收到 28，7 条逐档腿原样不动。
        """
        legs = [_tp("g%d" % i, 2600.0 + i * 10, -1) for i in range(7)]
        legs.append(_tp("m1", 2900.0, -29))
        notes, cli = self._run(28, legs)
        self.assertEqual([(2900.0, 28)], self._tp_placed(cli),
                         "只有全仓腿被收到持仓")
        self.assertEqual(["m1"], cli.cancelled, "逐档腿一律不动")
        self.assertEqual(1, len([n for n in notes if "clamped" in n]))
        self.assertEqual([], [n for n in notes if "resized" in n])

    def test_multiple_oversized_legs_still_apportioned(self):
        """**多条**腿都 ≥ 持仓 = 陈旧腿堆叠（非有意重叠）→ 仍走分摊，不做双重覆盖。

        持仓 1 却有两条 5 张：若误判成「全仓腿」会变成两条 1 张 = 双重覆盖；
        正确行为 = 分摊成 [1, 0]（一条出口覆盖持仓、另一条撤掉）。
        """
        notes, cli = self._run(1, [_tp("a", 2600.0, -5), _tp("b", 2650.0, -5)])
        self.assertEqual([(2600.0, 1)], self._tp_placed(cli))
        self.assertEqual(sorted(["a", "b"]), sorted(cli.cancelled))
        self.assertEqual([], [n for n in notes if "clamped" in n],
                         "多条超配腿不得走「全仓腿」分支")


class TestApportionSizes(unittest.TestCase):
    """`_apportion_sizes`（最大余数法）单元契约。"""

    def _ap(self, total, weights):
        from omnialpha.executor import _apportion_sizes
        return _apportion_sizes(total, weights)

    def test_sum_invariant(self):
        for total, weights in [(17, [6, 4]), (4, [2, 2, 2]), (1, [5, 5]),
                               (10, [1, 1, 1]), (7, [3, 3, 3]), (6, [1, 2, 3]),
                               (5, [10]), (3, [1, 1, 1, 1])]:
            sizes = self._ap(total, weights)
            self.assertEqual(len(weights), len(sizes))
            self.assertEqual(total, sum(sizes),
                             f"合计必须恒等于 total：{total} {weights} → {sizes}")
            self.assertTrue(all(s >= 0 for s in sizes))

    def test_degenerate(self):
        self.assertEqual([0, 0], self._ap(0, [3, 3]))
        self.assertEqual([0, 0], self._ap(5, [0, 0]))
        self.assertEqual([], self._ap(5, []))

    def test_deterministic(self):
        a = self._ap(17, [6, 4])
        b = self._ap(17, [6, 4])
        self.assertEqual(a, b)

    def test_largest_remainder_order(self):
        """6:4 分 17 → 10 + 7（b 余数 0.8 > a 余数 0.2，补 b）。"""
        self.assertEqual([10, 7], self._ap(17, [6, 4]))


# ───────────────────── P1：阶梯档位表落盘 ─────────────────────

class TestRecordLadderLevel(unittest.TestCase):
    """`_record_ladder_level`：原子 / 有界 / 失败不拦。"""

    def _ex(self, bot_id="martin-grid", root=None):
        td = root if root is not None else tempfile.TemporaryDirectory()
        if root is None:
            self.addCleanup(td.cleanup)
        cli = FakeClient()
        return Executor(cli, label_prefix="mtg", root=Path(td.name),
                        bot_id=bot_id)

    def _path(self, ex):
        return Path(ex.root) / "data" / "bots" / "martin-grid" / "state" / "ladder_levels.json"

    def _intent(self, **kw):
        kw.setdefault("price", 2400.0)      # 限价腿默认给档位价；市价腿传 price=None
        return Intent(action="open_long", symbol="ETH_USDT",
                      size=2, label="mtg",
                      meta={"tp_target": 2441.9}, **kw)

    def test_writes_level_fields(self):
        ex = self._ex()
        ex._record_ladder_level(self._intent(), 2, "oid-1")
        data = json.loads(self._path(ex).read_text(encoding="utf-8"))
        self.assertEqual(1, len(data["levels"]))
        lv = data["levels"][-1]
        self.assertEqual("ETH_USDT", lv["symbol"])
        self.assertEqual(2400.0, lv["price"])
        self.assertEqual(2, lv["size"])
        self.assertEqual(2441.9, lv["tp_target"])
        self.assertEqual("mtg", lv["label"])
        self.assertEqual("oid-1", lv["order_id"])
        self.assertEqual("pending", lv["status"])
        self.assertIn("ts", lv)

    def test_appends_and_bounds_to_200(self):
        ex = self._ex()
        for i in range(250):
            ex._record_ladder_level(self._intent(), 1, "oid-%d" % i)
        data = json.loads(self._path(ex).read_text(encoding="utf-8"))
        self.assertEqual(200, len(data["levels"]))
        self.assertEqual("oid-249", data["levels"][-1]["order_id"])

    def test_corrupt_file_rebuilt(self):
        ex = self._ex()
        p = self._path(ex)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        ex._record_ladder_level(self._intent(), 2, "oid-1")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(1, len(data["levels"]))

    def test_failure_never_raises(self):
        """root 指向文件 → mkdir 炸 → 吞掉（档位表是增强不是关键路径）。"""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        blocker = Path(td.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        ex = self._ex(root=type("R", (), {"name": str(blocker)})())
        # 不抛异常即通过；swallow 走 observability 计数
        ex._record_ladder_level(self._intent(), 2, "oid-1")

    def test_no_bot_id_is_noop(self):
        cli = FakeClient()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ex = Executor(cli, label_prefix="mtg", root=Path(td.name))  # 无 bot_id
        ex._record_ladder_level(self._intent(), 2, "oid-1")
        self.assertFalse((Path(td.name) / "data").exists())

    def _open_with_patches(self, intent):
        """跑 `_open`：重依赖（风控/前置校验/入场腿）全 mock，只锁 P1 接线。"""
        cli = FakeClient(positions=[])
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ex = Executor(cli, label_prefix="mtg", root=Path(td.name),
                      bot_id="martin-grid")
        patches = [
            mock.patch.object(Executor, "_check_symbol", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_safe_mode", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_no_flip", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_open_sl", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_account_risk", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_notional", lambda *a, **k: None),
            mock.patch.object(Executor, "_precheck_exit_triggers",
                              lambda *a, **k: ""),
            mock.patch.object(Executor, "_place_entry_leg",
                              lambda *a, **k: ({"order": {"id": "e1"}}, None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ex, ex._open(intent)

    def test_open_records_level_after_entry_confirmed(self):
        """集成：`_open` 入场单确认后落一档（限价腿有档位价）。"""
        ex, res = self._open_with_patches(self._intent())
        self.assertTrue(res.ok, res.error)
        data = json.loads(self._path(ex).read_text(encoding="utf-8"))
        self.assertEqual(1, len(data["levels"]))
        self.assertEqual("e1", data["levels"][-1]["order_id"])
        self.assertEqual(2441.9, data["levels"][-1]["tp_target"])

    def test_market_entry_not_recorded(self):
        """市价腿没有档位价 → **不落档**（否则档位表混进 price=0 的垃圾档）。"""
        ex, res = self._open_with_patches(
            self._intent(price=None, order_type="market"))
        self.assertTrue(res.ok, res.error)
        self.assertFalse(self._path(ex).exists(),
                         "市价入场腿不应写档位表")


# ───────────────────── P2：account 快照注入档位表 ─────────────────────

class TestAccountToolLadderLevels(unittest.TestCase):
    """account 工具把 `state/ladder_levels.json` 注入快照（LLM 对账输入）。

    路径与 executor 写入处同源：`<bot_root>/data/bots/<bot_id>/state/`
    （`__main__` 传的 bot_root 是**项目根**；缺 bot_id 才退回项目根 state/）。
    """

    def _run(self, levels=None, raw=None, bot_id=None, symbols=("ETH_USDT",)):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        bot_root = Path(td.name)
        if raw is not None or levels is not None:
            base = bot_root
            if bot_id:
                base = bot_root / "data" / "bots" / bot_id
            (base / "state").mkdir(parents=True, exist_ok=True)
            body = raw if raw is not None else json.dumps(
                {"levels": levels}, ensure_ascii=False)
            (base / "state" / "ladder_levels.json").write_text(
                body, encoding="utf-8")
        cli = FakeClient()
        res = run_tool(cli, "account", {"symbols": list(symbols)},
                       bot_root=bot_root, bot_id=bot_id or "",
                       symbols=list(symbols))
        return res

    def _lv(self, i, symbol="ETH_USDT"):
        return {"symbol": symbol, "price": 2400.0 - i, "size": 1,
                "tp_target": 2440.0, "label": "mtg", "order_id": "o%d" % i,
                "ts": "2026-10-15T00:00:00Z", "status": "pending"}

    def test_injects_levels_bot_scoped_path(self):
        """生产接线：executor 写 <root>/data/bots/<bot>/state/，account 同路径读。"""
        res = self._run(levels=[self._lv(0), self._lv(1), self._lv(2)],
                        bot_id="martin-grid")
        self.assertEqual(3, len(res["ladder_levels"]))
        self.assertEqual(2440.0, res["ladder_levels"][0]["tp_target"])

    def test_other_bots_levels_not_leaked(self):
        """bot 隔离：A 的档位表不该被 B 读到（账户隔离铁律）。"""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        bot_root = Path(td.name)
        a_dir = bot_root / "data" / "bots" / "bot-a" / "state"
        a_dir.mkdir(parents=True)
        (a_dir / "ladder_levels.json").write_text(
            json.dumps({"levels": [self._lv(0)]}, ensure_ascii=False),
            encoding="utf-8")
        cli = FakeClient()
        res = run_tool(cli, "account", {"symbols": ["ETH_USDT"]},
                       bot_root=bot_root, bot_id="bot-b",
                       symbols=["ETH_USDT"])
        self.assertEqual([], res["ladder_levels"])              # b 什么都看不到

    def test_symbol_filter(self):
        """只注入被问到的币（多币 bot 不该把 BTC 档塞进 ETH 快照）。"""
        res = self._run(levels=[self._lv(0, "ETH_USDT"),
                                self._lv(1, "BTC_USDT")],
                        bot_id="martin-grid", symbols=("ETH_USDT",))
        self.assertEqual(1, len(res["ladder_levels"]))
        self.assertEqual("ETH_USDT", res["ladder_levels"][0]["symbol"])

    def test_missing_file_empty_list(self):
        res = self._run(bot_id="martin-grid")
        self.assertEqual([], res["ladder_levels"])

    def test_corrupt_file_empty_list(self):
        res = self._run(raw="{not json", bot_id="martin-grid")
        self.assertEqual([], res["ladder_levels"])

    def test_truncated_to_last_50(self):
        res = self._run(levels=[self._lv(i) for i in range(60)],
                        bot_id="martin-grid")
        self.assertEqual(50, len(res["ladder_levels"]))
        self.assertEqual("o59", res["ladder_levels"][-1]["order_id"])

    def test_fallback_path_without_bot_id(self):
        """无 bot_id（老调用方）→ 退回项目根 state/，不拦。"""
        res = self._run(levels=[self._lv(0)])
        self.assertEqual(1, len(res["ladder_levels"]))


# ───────────────────── 人格层静态锁（防回退） ─────────────────────

class TestPersonaStatic(unittest.TestCase):
    """ladder_nogate.md 关键规则在位（人格在主仓库、未入 git，按路径解析）。

    引擎修复只保证「能改」；「每档自己的 TP、3 格马丁共用一个」是人格层规则，
    这里用静态检查防回退。文件缺失（如在纯代码环境跑）时跳过。
    """

    @classmethod
    def setUpClass(cls):
        # worktree: <main>/.worktrees/trigger-fix → 主仓库 = parents[1]
        p = ROOT.parents[1] / "prompts" / "ladder_nogate.md"
        cls.path = p if p.exists() else None
        cls.text = (p.read_text(encoding="utf-8").replace("\r\n", "\n")
                    if cls.path else "")

    def setUp(self):
        if not self.path:
            self.skipTest("persona file not found")

    def test_reconcile_section_present(self):
        self.assertIn("### 3.3 逐档止盈对账", self.text)

    def test_reconcile_inputs_documented(self):
        self.assertIn("ladder_levels", self.text)
        self.assertIn("open_orders", self.text)

    def test_three_way_table_present(self):
        for token in ("modify_tp_sl", "reduce_long", "等反弹"):
            self.assertIn(token, self.text)

    def test_structure_tp_rule(self):
        self.assertIn("范围内找结构", self.text)

    def test_share_rules_present(self):
        self.assertIn("tpN_share", self.text)
        self.assertIn("同轮所有档 share 之和必须 = 1.0", self.text)

    def test_hard_bans_present(self):
        self.assertIn("绝不发不带 `tpN_share` 的单 `tp` `modify_tp_sl`", self.text)
        self.assertIn("绝不给网格层设固定止盈值", self.text)
        self.assertIn("绝不能用无 share 的单 tp", self.text)

    def test_martin_shared_tp_covers_whole_position(self):
        """2026-10-15 放开：马丁共用 TP **单腿覆盖全仓（含网格）**，触发即全仓了结。

        旧文本「只覆盖马丁 3 档 / 网格不动」是假网格修复时的临时收紧，与
        §〇「触发时全部平仓（含网格）」自相矛盾，已按用户要求放开为单腿 share=1.0。
        """
        self.assertIn("马丁共用 TP = 单腿、覆盖全仓（含网格）", self.text)
        self.assertIn("平掉全部持仓（马丁 + 网格）", self.text)
        self.assertIn("`tp1_share: 1.0`", self.text)
        self.assertNotIn("网格不动", self.text)
        self.assertNotIn("只覆盖马丁 3 档", self.text)

    def test_grid_tp_still_only_closes_own(self):
        """放开马丁 ≠ 放开网格：网格档自己的 tp 仍只平自己那 N 张。"""
        self.assertIn("网格档的 tp 触发时**只减自己那 N 张**", self.text)
        self.assertIn("唯一例外 = 马丁共用 TP", self.text)

    def test_management_region_trend(self):
        self.assertIn("管理腿（`modify_tp_sl`/`reduce_long`）填 `trend`", self.text)

    def test_reduce_long_uses_contracts(self):
        """契约勘误（2026-10-15）：reduce_long 只认 `size`（张数）——
        写 size_usd 会被解析成 0 张 = 全平，人格必须说清。"""
        self.assertIn("`size = N`", self.text)
        self.assertIn("引擎只认 `size`", self.text)


if __name__ == "__main__":
    unittest.main()
