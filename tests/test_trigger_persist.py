"""触发器状态持久化 + 节拍闸门回归测试。

对应 fix/trigger-persist 分支修复的三个产线缺陷：
  R1  `AITriggerStore.mark_fired()` 死代码 → 触发已发生但 ai_triggers.json 纹丝不动；
  R2  冷却状态只在内存 → 进程重启后同一触发器立刻重触发（冷却形同虚设）；
  R3  事件轮不推进定时器 → 15 分钟节拍被 212/261s 的「补轮」打散。

判定方式：先造出「条件为真」的 K 线（最后收 2400 < 前 20 根最小 2500 →
price_break(side=low, lookback=20) 恒真），驱动真实 PlanRunner 代码路径。
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist import market as market_mod  # noqa: E402
from omnialpha.strategist import loop as loop_mod  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402

SYM = "ETH_USDT"


def _candles(n=120, base=2500.0, last=2400.0):
    rows = []
    t0 = 1_700_000_000
    for i in range(n):
        c = last if i == n - 1 else base
        rows.append({"t": t0 + i * 900, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 100.0})
    return rows


class _StubClient:
    def get_klines(self, symbol, interval, limit):
        return _candles()

    def public_get(self, path, query):
        return []


def _patch_market(case: unittest.TestCase) -> None:
    """就地打桩 `triggers._load_candles` 依赖的 `market.fetch_rest_candles`。

    **必须登记恢复**（2026-10-14 N2 发现）：此前直接赋值且从不还原，等于把
    「所有 symbol/interval 都返回这份 1970 年的 ETH 假 K 线」这个桩**永久**留在
    进程里 —— 同一进程后续跑的 `test_triggers*.py` 全部拿到错数据而失败
    （逐文件独立进程的 unittest 掩盖了它；pytest 单进程一跑就暴露 26 个失败）。
    """
    orig = market_mod.fetch_rest_candles
    market_mod.fetch_rest_candles = lambda client, symbol, interval, limit: _candles()
    case.addCleanup(setattr, market_mod, "fetch_rest_candles", orig)


def _runner(hist: Path, **ai) -> PlanRunner:
    cfg = StrategistConfig(
        symbols=[SYM],
        prompt_file="prompts/vergex_default.md",
        interval_sec=900,
        event_on_kline_close=False,
        ai_triggers={"enabled": True, "max_active": 5, "default_cooldown_sec": 900.0, **ai},
    )
    return PlanRunner(
        client=_StubClient(), cfg=cfg,
        inbox=hist / "inbox", history_dir=hist, llm=object(),
    )


TRIG = {"type": "price_break", "symbol": SYM, "lookback": 20, "side": "low",
        "cooldown_sec": 900, "ttl_sec": 86400, "reason": "跌破 20 根低点再看"}


class TestTriggerStatePersistence(unittest.TestCase):
    def setUp(self):
        _patch_market(self)

    def _add_trigger(self, runner):
        from omnialpha.strategist.trigger_store import validate_trigger_payload
        runner._ai_store.add(
            validate_trigger_payload(TRIG, runner._ai_store.policy, [SYM]), [SYM]
        )

    def test_r1_last_fire_persisted_after_trigger(self):
        """触发发生后，ai_triggers.json 里的 last_fire 必须被回写（R1）。"""
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td)
            runner = _runner(hist)
            self._add_trigger(runner)

            path = hist / "ai_triggers.json"
            before = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(before["items"][0]["last_fire"], 0.0)

            out = runner._check_condition_events()
            self.assertIsNotNone(out)  # 条件为真 → 触发

            after = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreater(after["items"][0]["last_fire"], 0.0)
            self.assertGreater(after["updated_at"], before["updated_at"])

    def test_r2_cooldown_survives_restart(self):
        """进程重启后冷却仍然生效：新实例第一次 check 不触发（R2）。"""
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td)
            r1 = _runner(hist)
            self._add_trigger(r1)
            self.assertIsNotNone(r1._check_condition_events())

            # 模拟重启：全新 PlanRunner 读同一份 history_dir
            r2 = _runner(hist)
            self.assertEqual(len(r2._ai_store.active()), 1)
            self.assertGreater(r2._ai_store.active()[0].last_fire, 0.0)
            self.assertIsNone(r2._check_condition_events())

    def test_r2b_last_true_roundtrip(self):
        """边沿判据 last_true 也要跨进程：仍为真的状态型条件重启后不补报。"""
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td)
            r1 = _runner(hist)
            self._add_trigger(r1)
            self.assertIsNotNone(r1._check_condition_events())

            r2 = _runner(hist)
            self.assertTrue(r2._ai_store.active()[0].last_true)
            # K 线不变（条件仍为真），重启后也不应立刻补报
            self.assertIsNone(r2._check_condition_events())

    def test_state_file_unchanged_when_no_trigger(self):
        """没有触发器时 check 不写盘、不报错。"""
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td)
            runner = _runner(hist)
            self.assertIsNone(runner._check_condition_events())


class TestCondGapGate(unittest.TestCase):
    def setUp(self):
        _patch_market(self)

    def test_cond_gap_default_is_half_interval(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _runner(Path(td))
            self.assertEqual(runner._cond_gap_sec(), 450.0)

    def test_cond_gap_explicit_override(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _runner(Path(td))
            runner.cfg.cond_gap_sec = 300.0
            self.assertEqual(runner._cond_gap_sec(), 300.0)

    def test_r3_event_round_advances_timer(self):
        """事件轮后，下一个 interval 轮推迟到 cond+900（R3：不再在 900 处补轮）。"""
        class _Stop(BaseException):
            pass

        class _FakeTime:
            def __init__(self, start, budget):
                self.t = float(start)
                self.start = float(start)
                self.budget = float(budget)

            def time(self):
                return self.t

            def sleep(self, s):
                self.t += max(float(s), 0.5)
                if self.t - self.start >= self.budget:
                    raise _Stop()

        with tempfile.TemporaryDirectory() as td:
            runner = _runner(Path(td), default_cooldown_sec=900.0)
            runner.cfg.check_interval_sec = 1.0

            ft = _FakeTime(start=1_000_000.0, budget=2000.0)
            rounds = []

            def _run_once(trigger="manual", **kw):
                rounds.append((ft.t - ft.start, trigger))
                return {}

            # 模拟真实触发器语义：t=610 条件首次为真，自带 900s 冷却
            # （与 check_conditions 的 st.last_fire 行为一致，否则 stub 会绕过冷却）
            last_fire = [-1e18]

            def _check():
                t = ft.t - ft.start
                if t >= 610 and t - last_fire[0] >= 900:
                    last_fire[0] = t
                    return "cond[price_break:ETH_USDT:x]"
                return None

            runner.run_once = _run_once
            runner._check_condition_events = _check
            saved = loop_mod.time
            loop_mod.time = ft
            try:
                runner.run_forever()
            except _Stop:
                pass
            finally:
                loop_mod.time = saved

            kinds = [g.split("[")[0] for _, g in rounds]
            times = [t for t, _ in rounds]
            self.assertEqual(kinds[0], "interval")   # t=0 定时轮
            self.assertEqual(kinds[1], "cond")       # t=610 事件轮
            # t=900（原节拍点）不应再有轮 —— cond 轮已把定时器推进到 610，下一轮在 1510
            self.assertNotIn(900.0, times)
            self.assertEqual(kinds[2], "interval")   # t=1510 = 610 + 900
            # 任何轮距都不得小于 cond_gap 闸门（450s）
            gaps = [round(b - a, 3) for a, b in zip(times, times[1:])]
            self.assertTrue(all(g >= 450.0 for g in gaps), gaps)


if __name__ == "__main__":
    unittest.main()
