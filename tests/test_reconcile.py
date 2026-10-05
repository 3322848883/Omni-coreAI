"""对账：保护单张数对齐持仓（保护单堆积的引擎侧兜底）。

回归背景（2026-10-05 架构盘点）：每轮重挂入场单都会各留一组 TP/SL，而
`executor._is_orphan_protector` 只按**方向**判定（有同向持仓就保留、不看张数）
→ 保护单**只增不减**。实测 eth-disc 6 小时堆到 30 个 / 202 张 vs 4 张持仓。
唯一能对齐张数的 `_resync_protectors` 只在平/减仓路径调用，加仓路径没人管。

fail-closed 是本模块的核心约束：取不到交易所报告、无持仓、有未成交入场单
→ **都不动**（撤错了是裸仓，比堆积严重得多）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.reconcile import reconcile_protectors  # noqa: E402


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


def _prot(pid, size, tail="tp", created=1.0, text="t-b1-x"):
    return {
        "id": pid,
        "create_time": created,
        "initial": {"contract": "BTC_USDT", "size": -abs(size),
                    "is_reduce_only": True, "text": f"{text}-{tail}"},
    }


def _pos(size=4):
    return {"contract": "BTC_USDT", "size": size, "entry_price": 50000.0}


class TestReconcileProtectors(unittest.TestCase):
    def _ex(self, client):
        return Executor(client, symbols_whitelist=["BTC_USDT"],
                        label_prefix="b1", bot_id="b1")

    def test_drops_excess_keeping_newest(self):
        """持仓 4 张、保护单合计 30 张 → 保留最新的够 4 张，其余撤。"""
        client = _Client(
            positions=[_pos(4)],
            protectors=[_prot(1, 12, created=1.0), _prot(2, 6, created=2.0),
                        _prot(3, 4, created=3.0), _prot(4, 8, created=4.0)],
        )
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertTrue(res["ok"])
        self.assertEqual(res["position_size"], 4)
        # 最新的是 id=4（size 8 ≥ 4）→ 保留它，撤其余三个
        self.assertEqual(client.cancelled, ["3", "2", "1"])
        self.assertEqual(res["kept"], 1)

    def test_no_cancel_when_sizes_match(self):
        """张数刚好够 → 一笔都不撤。"""
        client = _Client(positions=[_pos(4)], protectors=[_prot(1, 4, created=1.0)])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(res["kept"], 1)

    def test_flat_does_nothing(self):
        """无持仓 → 不动（交给 `_cleanup_orphan_protectors`）。"""
        client = _Client(positions=[], protectors=[_prot(1, 9), _prot(2, 9)])
        res = reconcile_protectors(self._ex(client), "BTC_USDT")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(res.get("skipped"), "no_position")

    def test_pending_entry_raises_target(self):
        """待成交入场单也计入目标张数 —— 保护单要覆盖它（否则成交即裸仓）。

        实测场景：0 持仓 + 7 张待成交入场单 + 31 张保护单 —— 保护单明显超额，
        但「有 pending entry 就整体跳过」会让它永远清不掉。
        """
        client = _Client(positions=[], protectors=[
            _prot(1, 7, created=1.0), _prot(2, 12, created=2.0),
            _prot(3, 12, created=3.0)])
        ex = self._ex(client)
        ex._owned_open_orders = lambda sym, prefix: [
            {"size": 7, "left": 7, "is_reduce_only": False}]
        res = reconcile_protectors(ex, "BTC_USDT")
        self.assertEqual(res["position_size"], 0)
        self.assertEqual(res["pending_size"], 7)
        self.assertEqual(res["target_size"], 7)
        # 最新的是 id=3（12 张 ≥ 7）→ 保留它，撤 2、1
        self.assertEqual(sorted(client.cancelled), ["1", "2"])

    def test_group_is_kept_whole(self):
        """同一组（同秒创建）必须整组保留 —— 不能只留 SL 把 TP 撤掉。

        实测踩到：`_open` 连续挂 TP 再挂 SL（相差不到 1 秒），按单倒序累加时
        最新的 SL 先被选中、凑够张数就停 → TP 被撤，保护不完整。
        """
        client = _Client(positions=[], protectors=[
            # 旧组：tp + sl 同秒
            _prot(1, 12, tail="tp", created=100.0),
            _prot(2, 12, tail="sl", created=100.0),
            # 新组：tp + sl 同秒（比旧组新）
            _prot(3, 7, tail="tp", created=200.0),
            _prot(4, 7, tail="sl", created=200.0),
        ])
        ex = self._ex(client)
        ex._owned_open_orders = lambda sym, prefix: [
            {"size": 7, "left": 7, "is_reduce_only": False}]
        res = reconcile_protectors(ex, "BTC_USDT")
        # 新组整组保留（tp 3 + sl 4），旧组整组撤掉（1、2）
        self.assertEqual(sorted(client.cancelled), ["1", "2"])
        self.assertEqual(res["kept"], 2, "应保留整组（TP+SL）而不是单张 SL")

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


if __name__ == "__main__":
    unittest.main()
