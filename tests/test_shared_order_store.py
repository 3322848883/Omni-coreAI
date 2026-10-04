# -*- coding: utf-8 -*-
"""共享订单库的读健壮性（多 bot 共享记忆的底座）。

`data/shared/orders/` 是**多 bot 共享**的：persona 组在写，同时可能还有
独立跑 plan 的成员 bot 在读（同一个 bot 既跑 persona-run 又跑 plan-loop 时就是如此）。
Windows 上并发读写会**瞬时**失败（`os.replace` 期间的共享冲突）。

原先 `get()` 把所有异常都吞成 None → 调用方当成「订单不存在」→ 抛 KeyError；
`list_open()` 更糟，会静默跳过读不到的文件 → bot 看到「无持仓」。
实测：4 个进程并发写同一张单，7/8 直接 `KeyError: order not found`。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.persona.orders import SharedOrderStore

OID = "o-read0001"


def _store(td: str) -> SharedOrderStore:
    store = SharedOrderStore(Path(td))
    store.create({"order_id": OID, "symbol": "BTC_USDT", "side": "long",
                  "group": "g", "members": ["m1"], "target_account": "m1",
                  "status": "open"})
    return store


def _flaky_read_text(fail_times: int):
    """让 Path.read_text 前 N 次对 .json 抛 PermissionError（模拟共享冲突）。"""
    real = Path.read_text
    state = {"n": 0}

    def wrapper(self, *a, **kw):
        if self.suffix == ".json" and state["n"] < fail_times:
            state["n"] += 1
            raise PermissionError("sharing violation (simulated)")
        return real(self, *a, **kw)

    return wrapper


class TestSharedOrderReadRobustness(unittest.TestCase):
    def test_get_retries_transient_failure(self):
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            with mock.patch.object(Path, "read_text", _flaky_read_text(2)):
                rec = store.get(OID)
            self.assertIsNotNone(rec, "一次瞬时读失败被当成了「订单不存在」")
            self.assertEqual(rec["order_id"], OID)

    def test_list_open_does_not_silently_drop(self):
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            with mock.patch.object(Path, "read_text", _flaky_read_text(2)):
                opens = store.list_open()
            self.assertEqual([r["order_id"] for r in opens], [OID],
                             "list_open 静默丢掉了读瞬时失败的订单（bot 会看到「无持仓」）")

    def test_record_vote_survives_transient_failure(self):
        """原先这里会抛 `KeyError: order not found`（实测并发场景下 7/8 崩溃）。"""
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            with mock.patch.object(Path, "read_text", _flaky_read_text(2)):
                store.record_vote(OID, "c-1", "m1", "long", reasoning="x", confidence=0.5)
            self.assertIn("c-1", store.get(OID)["votes"])

    def test_missing_order_still_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            self.assertIsNone(store.get("o-nope"))

    def test_persistent_failure_still_returns_none(self):
        """持续读不到（不是瞬时）→ 返回 None，不无限重试。"""
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            with mock.patch.object(Path, "read_text", _flaky_read_text(10_000)):
                self.assertIsNone(store.get(OID))


if __name__ == "__main__":
    unittest.main()
