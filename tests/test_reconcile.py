"""对账：保护单**去重**（只裁同向同价的重复腿，保留不同触发价的多级 TP）。

回归背景（2026-10-05 架构盘点）：每轮重挂入场单都会各留一组 TP/SL，而
`executor._is_orphan_protector` 只按**方向**判定（有同向持仓就保留、不看张数）
→ 保护单**只增不减**。实测 eth-disc 6 小时堆到 30 个 / 202 张 vs 4 张持仓。
唯一能对齐张数的 `_resync_protectors` 只在平/减仓路径调用，加仓路径没人管。

**判据于 2026-10-15 重写**（原「按张数裁到 持仓+待成交」）：SL 单张就覆盖整个
目标（`_sl_cover_cap`），原判据因此**周期性吃掉逐档 TP**（做 T 腿）—— 实测
11:40:55 把马丁共用 TP 的三条腿（6/7/9 张）裁成一条、并会撤掉 `ensure_grid_tp`
挂的逐档腿。新判据：

1. SL（`sl`/`ls`）：单层风控意图 → 每方向只留最新一条；
2. TP（`tp`/`lp`）：**按触发价保留**，只裁「同向+同价+同文本」的重复腿，
   保留其中 `|size|` 最大的一条（并列取最新）；**不做张数合并/重算**；
3. 安全阀：不同触发价的 TP 腿上限 `_MAX_DISTINCT_TP_LEGS`（超出从最旧撤）。

fail-closed 仍是核心约束：取不到交易所报告、无持仓、有未成交入场单 → **都不动**
（撤错了是裸仓，比堆积严重得多）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.reconcile import (  # noqa: E402
    _MAX_DISTINCT_TP_LEGS, reconcile_protectors)


class _Client:
    def __init__(self, positions=None, protectors=None, fail=False):
        self._positions = positions or []
        self._protectors = protectors or []
        self.fail = fail
        self.cancelled = []

    def banner(self):
        return ""

    def get_positions(self):
        if self.fail:
            raise RuntimeError("account down")
        return self._positions

    def list_price_orders(self, contract=None):
        if self.fail:
            raise RuntimeError("orders down")
        return self._protectors

    def cancel_price_order(self, oid):
        self.cancelled.append(str(oid))
        return {"id": oid}


def _prot(pid, size, tail="tp", created=1.0, text="t-b1-x", trig=None):
    d = {
        "id": pid,
        "create_time": created,
        "initial": {"contract": "BTC_USDT", "size": -abs(size),
                    "is_reduce_only": True, "text": f"{text}-{tail}"},
    }
    if trig is not None:
        d["trigger"] = {"price": trig, "rule": 2 if tail in ("sl", "ls") else 1}
    return d


def _pos(size=4):
    return {"contract": "BTC_USDT", "size": size, "entry_price": 50000.0}


class TestReconcileProtectors(unittest.TestCase):
    def _ex(self, client):
        return Executor(client, symbols_whitelist=["BTC_USDT"],
                        label_prefix="b1", bot_id="b1")

    # ── 核心回归：不同触发价的多级 TP 一律保留 ────────────────────────

    def test_distinct_triggers_all_kept(self):
        """**逐档止盈（做 T 腿）不能被当「超额」撤掉** —— 2026-10-15 实测事故。

        账户：SL 一条（覆盖 29 张）+ 逐档 TP 5 条（触发价各异，各 1-2 张）。
        旧判据：SL 单张就 ≥ 目标 29 → 只留与最新 SL 同 30s 窗口的那条 TP，
        其余整组撤（实测把马丁共用 TP 的三条腿裁成一条）。
        新判据：TP 按触发价保留 → **一条都不该撤**。
        """
        client = _Client(positions=[_pos(29)], protectors=[
            _prot(1, 29, tail="sl", created=1000.0, trig=2185.0),
            _prot(2, 1, tail="tp", created=1010.0, trig=2367.0),
            _prot(3, 1, tail="tp", created=1020.0, trig=2349.0),
            _prot(4, 1, tail="tp", created=1030.0, trig=2314.5),
            _prot(5, 2, tail="tp", created=1040.0, trig=2298.0),
            _prot(6, 1, tail="tp", created=1050.0, trig=2251.0),
        ])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertTrue(res["ok"])
        self.assertEqual(client.cancelled, [], "不同触发价的多级 TP 必须全部保留")
        self.assertEqual(res["kept"], 6)
        self.assertEqual(res["tp_legs"], 5)

    def test_grid_tp_plus_shared_tp_kept(self):
        """逐档 TP 与马丁共用 TP（同文本、不同触发价）共存 → 都保留。"""
        client = _Client(positions=[_pos(6)], protectors=[
            _prot(1, 6, tail="sl", created=1000.0, trig=2185.0),
            _prot(2, 6, tail="tp", created=1010.0, trig=2408.5),   # 马丁共用
            _prot(3, 1, tail="tp", created=1020.0, trig=2367.0),   # 逐档
            _prot(4, 1, tail="tp", created=1030.0, trig=2349.0),   # 逐档
        ])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(res["tp_legs"], 3)

    # ── 同价同文本的重复腿：只留覆盖最大的一条 ────────────────────────

    def test_same_price_duplicates_keep_max_cover(self):
        """同触发价 + 同文本的多条 = 重复挂单 → 只留 `|size|` 最大的一条。

        马丁三档各挂一条同价共用 TP（6/7/9 张）就是这种形态：保留覆盖最大的
        那条（9），撤掉 6、7。张数**不合并**（合并/重算是 `_align_tp_group` 的活）。
        """
        client = _Client(positions=[_pos(6)], protectors=[
            _prot(1, 6, tail="tp", created=1000.0, trig=2408.5),
            _prot(2, 7, tail="tp", created=1010.0, trig=2408.5),
            _prot(3, 9, tail="tp", created=1020.0, trig=2408.5),
        ])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, ["2", "1"])
        self.assertEqual(res["kept"], 1)
        self.assertEqual(res["dedup"], 2)

    def test_same_price_tie_keeps_newest(self):
        """同价同文本且张数相同 → 留最新那条。"""
        client = _Client(positions=[_pos(4)], protectors=[
            _prot(1, 4, tail="tp", created=1000.0, trig=2408.5),
            _prot(2, 4, tail="tp", created=1200.0, trig=2408.5),
        ])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, ["1"])
        self.assertEqual(res["kept"], 1)

    def test_different_text_same_price_both_kept(self):
        """同价但**文本不同**（不同策略/命名空间）→ 不算重复，都保留。"""
        client = _Client(positions=[_pos(4)], protectors=[
            _prot(1, 4, tail="tp", created=1000.0, text="t-b1-a", trig=2408.5),
            _prot(2, 4, tail="tp", created=1010.0, text="t-b1-b", trig=2408.5),
        ])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(res["tp_legs"], 2)

    # ── SL：单层风控意图 ────────────────────────────────────────────

    def test_sl_multi_price_keeps_newest(self):
        """SL 多价并存 = 提前平仓风险 → 每方向只留最新一条（与旧实现一致）。"""
        client = _Client(positions=[_pos(4)], protectors=[
            _prot(1, 4, tail="sl", created=1000.0, trig=2190.0),
            _prot(2, 4, tail="sl", created=1200.0, trig=2185.0),
        ])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, ["1"])
        self.assertEqual(res["kept"], 1)
        self.assertEqual(res["tp_legs"], 0)

    def test_sl_same_price_duplicates_deduped(self):
        """同价 SL 分腿（实测 4 条 9/1/1/6）→ 只留最新一条。"""
        client = _Client(positions=[_pos(9)], protectors=[
            _prot(1, 9, tail="sl", created=1000.0, trig=2325.0),
            _prot(2, 1, tail="sl", created=1010.0, trig=2325.0),
            _prot(3, 6, tail="sl", created=1020.0, trig=2325.0),
        ])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, ["2", "1"])
        self.assertEqual(res["dedup"], 2)

    # ── 安全阀 ──────────────────────────────────────────────────────

    def test_tp_cap_safety_valve(self):
        """不同触发价的 TP 腿超过上限 → 从**最旧**开始撤（防每轮在新价位攒一条）。"""
        prot = [_prot(1, 5, tail="sl", created=900.0, trig=2185.0)]
        n = _MAX_DISTINCT_TP_LEGS + 2
        for i in range(n):                      # 触发价各异，创建时间递增
            prot.append(_prot(100 + i, 1, tail="tp", created=1000.0 + i * 60,
                              trig=2300.0 + i))
        client = _Client(positions=[_pos(5)], protectors=prot)
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(res["capped"], 2)
        # 最旧的两条（100、101）被撤
        self.assertEqual(client.cancelled, ["100", "101"])
        self.assertEqual(res["tp_legs"], _MAX_DISTINCT_TP_LEGS)

    # ── 既有语义保持不变 ────────────────────────────────────────────

    def test_no_cancel_when_single_legs(self):
        """各一条 TP/SL（无重复）→ 一笔都不撤。"""
        client = _Client(positions=[_pos(4)], protectors=[
            _prot(1, 4, tail="tp", created=1.0, trig=51000.0),
            _prot(2, 4, tail="sl", created=1.0, trig=49000.0),
        ])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(res["kept"], 2)

    def test_flat_does_nothing(self):
        """无持仓 → 不动（交给 `_cleanup_orphan_protectors`）。"""
        client = _Client(positions=[], protectors=[_prot(1, 9), _prot(2, 9)])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(res.get("skipped"), "no_position")

    def test_pending_entry_raises_target(self):
        """待成交入场单也计入目标张数 —— 否则「无持仓但有待成交」会被整体跳过。

        实测场景：0 持仓 + 7 张待成交入场单 + 31 张保护单 —— 保护单明显堆积，
        但「有 pending entry 就整体跳过」会让它永远清不掉。
        """
        client = _Client(positions=[], protectors=[
            _prot(1, 7, tail="tp", created=1000.0),
            _prot(2, 12, tail="tp", created=1060.0),
            _prot(3, 12, tail="tp", created=1120.0)])
        ex = self._ex(client)
        ex._owned_open_orders = lambda sym, prefix: [
            {"size": 7, "left": 7, "is_reduce_only": False}]
        res = reconcile_protectors(ex, "BTC_USDT")
        self.assertEqual(res["position_size"], 0)
        self.assertEqual(res["pending_size"], 7)
        self.assertEqual(res["target_size"], 7)
        # 三条同价（无 trigger 字段 → 同 key）→ 留覆盖最大的 id=3（并列取最新）
        self.assertEqual(sorted(client.cancelled), ["1", "2"])

    def test_tp_and_sl_are_independent(self):
        """同一次 `_open` 的 TP 与 SL 互不影响（tail 不同 → 不同判据）。"""
        client = _Client(positions=[], protectors=[
            _prot(1, 10, tail="tp", created=1000.0, trig=51000.0),
            _prot(2, 10, tail="sl", created=1001.7, trig=49000.0),
        ])
        ex = self._ex(client)
        ex._owned_open_orders = lambda sym, prefix: [
            {"size": 10, "left": 10, "is_reduce_only": False}]
        res = reconcile_protectors(ex, "BTC_USDT")
        self.assertEqual(client.cancelled, [], "TP/SL 各一条都不该被撤")
        self.assertEqual(res["kept"], 2)

    def test_pending_stop_entry_counts(self):
        """**条件单**里的入场类也要计入待成交张数。

        这个坑在本项目出现过三次（`_has_pending_entry` 最初漏条件单 → 孤儿
        扫描误撤预挂保护单 → 委托一成交即裸仓）。eth-disc 的入场单正是条件单
        （`stop_entry_*` 挂在 `/price_orders`），漏扫会让 `pending_size=0`、
        对账直接跳过 —— 实测保护单 30 张 vs 目标 0，一直清不掉。
        """
        client = _Client(positions=[], protectors=[
            _prot(1, 10, tail="sl", created=1000.0, trig=49000.0),   # 旧 SL
            _prot(2, 10, tail="tp", created=1200.0, trig=51000.0),
            _prot(3, 10, tail="sl", created=1200.0, trig=49500.0),   # 新 SL
        ])
        ex = self._ex(client)
        ex._owned_open_orders = lambda sym, prefix: []       # 无普通挂单
        ex._owned_price_orders = lambda sym, prefix: [       # 但有一个入场条件单
            {"id": "e1", "status": "open",
             "initial": {"contract": "BTC_USDT", "size": 10,
                         "is_reduce_only": False, "text": "t-b1-entry"}},
        ]
        res = reconcile_protectors(ex, "BTC_USDT")
        self.assertEqual(res["pending_size"], 10, "条件单入场单必须计入")
        self.assertEqual(res["target_size"], 10)
        # SL 多价 → 只留最新（id=3），旧 SL（id=1）撤
        self.assertEqual(client.cancelled, ["1"])

    def test_reduce_only_condition_not_counted(self):
        """reduce_only 的条件单是保护单，不是入场单 —— 不能计入待成交。"""
        client = _Client(positions=[], protectors=[])
        ex = self._ex(client)
        ex._owned_open_orders = lambda sym, prefix: []
        ex._owned_price_orders = lambda sym, prefix: [
            {"id": "p1", "status": "open",
             "initial": {"contract": "BTC_USDT", "size": -10,
                         "is_reduce_only": True, "text": "t-b1-sl"}},
        ]
        res = reconcile_protectors(ex, "BTC_USDT")
        self.assertEqual(res["pending_size"], 0)
        self.assertEqual(res.get("skipped"), "no_position")

    def test_flat_and_no_pending_does_nothing(self):
        """既无持仓也无待成交 → 不动（那是 `_cleanup_orphan_protectors` 的活）。"""
        client = _Client(positions=[], protectors=[_prot(1, 30, created=1.0)])
        ex = self._ex(client)
        ex._owned_open_orders = lambda sym, prefix: []
        res = reconcile_protectors(ex, "BTC_USDT")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(res.get("skipped"), "no_position")

    def test_fail_closed_on_data_error(self):
        """取数失败 → 不动任何单（绝不把 unknown 当 flat）。"""
        client = _Client(positions=[_pos(4)], protectors=[_prot(1, 30), _prot(2, 30)],
                         fail=True)
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertFalse(res["ok"])
        self.assertEqual(client.cancelled, [])

    def test_only_own_label_touched(self):
        """只撤本 bot 命名空间的单（别的 bot 的保护单不能碰）。"""
        client = _Client(
            positions=[_pos(4)],
            protectors=[_prot(1, 30, created=1.0, text="t-other"),
                        _prot(2, 30, created=2.0, text="t-other")],
        )
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, [], "不该碰别的 bot 的保护单")

    def test_non_reduce_only_ignored(self):
        """入场类条件单不是保护单，不参与对账。"""
        client = _Client(positions=[_pos(4)], protectors=[{
            "id": "9", "create_time": 1.0,
            "initial": {"contract": "BTC_USDT", "size": 10,
                        "is_reduce_only": False, "text": "t-b1-entry"},
        }])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, [])

    def test_alert_records_dedup_breakdown(self):
        """清理时落盘告警，并带上「重复/超上限/多级 TP」明细（可归因）。"""
        alerts = []

        class _Store:
            def raise_alert(self, kind, msg, **kw):
                alerts.append((kind, msg, kw))

        client = _Client(positions=[_pos(6)], protectors=[
            _prot(1, 6, tail="tp", created=1000.0, trig=2408.5),
            _prot(2, 9, tail="tp", created=1010.0, trig=2408.5),
        ])
        ex = self._ex(client)
        ex.alert_store = _Store()
        reconcile_protectors(ex, "BTC_USDT")
        self.assertEqual(len(alerts), 1)
        kind, msg, kw = alerts[0]
        self.assertEqual(kind, "orphan_protector")
        self.assertIn("重复腿", msg)
        self.assertEqual(kw["dedup"], 1)
        self.assertEqual(kw["tp_legs"], 1)
        self.assertEqual(kw["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
