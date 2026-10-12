"""run_forever 忙时不丢事件 + kline_tf 兜底 回归测试。

对应两个产线缺陷：
  L1  抢不到 `_busy` 就 `continue`：kline 基线已被 `_kline_closed()` 消费、
      该次收盘唤醒永久丢失（实测丢 11%），且 `continue` 跳过 `sleep` 忙等空转；
  L2  模型不填 `kline_tf`（多版本实测恒 None），审计/回放无法回答"读的是哪个周期"。

判定方式：不 mock 内部函数，驱动真实 `run_forever` 循环——
测试先持住 `_busy`，若实现仍在锁外评估触发（旧 bug），`_kline_closed()` 的
调用计数会立刻 >0 且 `sleep` 计数停在 0（忙等）；释放锁后事件必须补跑。
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist import loop as loop_mod  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402

SYM = "ETH_USDT"


class _StubClient:
    def get_klines(self, symbol, interval, limit):
        return []

    def public_get(self, path, query):
        return []


def _runner(tmp: str, **kw) -> PlanRunner:
    cfg = StrategistConfig(
        symbols=[SYM],
        prompt_file="prompts/vergex_default.md",
        interval_sec=0,            # 关掉 interval 节拍，单独测 kline 路径
        event_on_kline_close=True,
        check_interval_sec=0.01,
        ai_triggers={"enabled": False},
        **kw,
    )
    return PlanRunner(
        client=_StubClient(), cfg=cfg,
        inbox=Path(tmp) / "inbox", history_dir=Path(tmp), llm=object(),
    )


class _StopLoop(Exception):
    pass


class TestBusyDoesNotLoseEvent(unittest.TestCase):
    """L1：锁被占时不得消费事件、不得忙等；释放后事件必须补跑。"""

    def _make_runner(self, tmp):
        runner = _runner(tmp)
        state = {"kline_calls": 0, "ran": [], "sleeps": 0, "stop": threading.Event()}
        lock = runner._busy

        def _kline_closed():          # 模拟真实语义：事件只出现一次，被消费就没了
            state["kline_calls"] += 1
            return state["kline_calls"] == 1

        def _run_once(trigger=""):
            state["ran"].append(trigger)
            return "ok"

        real_sleep = time.sleep

        def _sleep(sec):
            state["sleeps"] += 1
            if state["stop"].is_set():
                raise _StopLoop()
            real_sleep(min(float(sec), 0.02))

        runner._kline_closed = _kline_closed
        runner.run_once = _run_once
        return runner, state, lock, _sleep

    def test_busy_then_release_event_not_lost(self):
        with tempfile.TemporaryDirectory() as td:
            runner, state, lock, _sleep = self._make_runner(td)
            lock.acquire()                      # 模拟 supervisor/上一轮正在跑
            try:
                with mock.patch.object(loop_mod.time, "sleep", _sleep):
                    t = threading.Thread(target=runner.run_forever, daemon=True)
                    t.start()
                    try:
                        # 忙相：最多等 1s，sleep 必须持续推进（旧代码 continue 跳过 sleep → 恒 0）
                        deadline = time.time() + 1.0
                        while time.time() < deadline and state["sleeps"] < 3:
                            state["stop"].wait(0.01)
                        self.assertGreaterEqual(state["sleeps"], 3, "忙等：continue 跳过了 sleep")
                        self.assertEqual(state["kline_calls"], 0,
                                         "锁被占时仍评估了触发（旧 bug：锁外消费 kline 基线）")
                        self.assertEqual(state["ran"], [], "锁被占时不该跑任何一轮")

                        lock.release()          # 释放 → 同一个事件必须补跑
                        deadline = time.time() + 3.0
                        while time.time() < deadline and not state["ran"]:
                            state["stop"].wait(0.01)
                        self.assertEqual(state["ran"], ["kline_close"], "释放锁后事件没补跑 = 丢失")
                    finally:
                        state["stop"].set()     # 必须在 patch 活着时发停止信号
                        t.join(2.0)
                self.assertFalse(t.is_alive())
            finally:
                if lock.locked():               # 清理，防测试自身死锁
                    try:
                        lock.release()
                    except RuntimeError:
                        pass

    def test_normal_path_still_fires(self):
        """无竞争时行为不变：kline 收盘立刻跑一轮。"""
        with tempfile.TemporaryDirectory() as td:
            runner, state, lock, _sleep = self._make_runner(td)
            with mock.patch.object(loop_mod.time, "sleep", _sleep):
                t = threading.Thread(target=runner.run_forever, daemon=True)
                t.start()
                try:
                    deadline = time.time() + 3.0
                    while time.time() < deadline and not state["ran"]:
                        state["stop"].wait(0.01)
                    self.assertEqual(state["ran"], ["kline_close"])
                finally:
                    state["stop"].set()
                    t.join(2.0)
                self.assertFalse(t.is_alive())


class TestKlineTfFallback(unittest.TestCase):
    """L2：kline_tf 为空的 chips 用 cfg.timeframe 兜底；已填的不覆盖。"""

    def test_empty_filled_existing_kept(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _runner(td, timeframe="15m")
            plan = SimpleNamespace(chips=[
                SimpleNamespace(kline_tf=""),
                SimpleNamespace(kline_tf="1h"),
                SimpleNamespace(kline_tf=None),
            ])
            runner._fill_kline_tf(plan)
            self.assertEqual(plan.chips[0].kline_tf, "15m")
            self.assertEqual(plan.chips[1].kline_tf, "1h")   # 已填 → 不覆盖
            self.assertEqual(plan.chips[2].kline_tf, "15m")

    def test_no_timeframe_noop(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _runner(td, timeframe="")
            plan = SimpleNamespace(chips=[SimpleNamespace(kline_tf="")])
            runner._fill_kline_tf(plan)
            self.assertEqual(plan.chips[0].kline_tf, "")

    def test_no_chips_noop(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _runner(td, timeframe="15m")
            runner._fill_kline_tf(SimpleNamespace(chips=None))   # 不抛即过
            runner._fill_kline_tf(SimpleNamespace())             # 无 chips 属性


if __name__ == "__main__":
    unittest.main()
