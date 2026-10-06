"""边沿触发：状态型条件只在 **False → True** 那一刻报一次。

为什么必须加（2026-10-06 线上实测）：条件分两类 —— 事件型比较「现在 vs 上一根」，
True 本身就是一瞬间；而状态型只看「现在是否成立」，成立期间每次求值都为真。
对状态型只按 `cooldown_sec` 去重，数学上等于一个定时器。

实测后果：`brooks-btc` 在 196 分钟里被 `price_vs_ema` 唤醒 **35 次**（占全部 82 轮的
43%），因为价格连续几小时在 EMA20 上方 → 每 300 秒冷却一到就再响一次。
2.5 小时烧掉约 **1005 万 prompt token**（约 $2.68）。

加边沿后 `price_vs_ema{side:above}` 的语义变成「价格**穿到** EMA 上方时叫醒我」——
正是模型设它时想要的。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.triggers import check_conditions  # noqa: E402

T0 = 1_790_000_000.0    # 真实量级的 epoch（last_fire 初值 0.0，冷却从纪元算起）
ASC = [float(i) for i in range(1, 40)]          # 价格在 EMA 上方
DESC = [float(i) for i in range(39, 0, -1)]     # 价格在 EMA 下方


class MutMD:
    """可换 K 线的桩件 —— 用来模拟「状态翻转」。"""

    def __init__(self, closes):
        self.closes = list(closes)

    def public_get(self, path, qs=""):
        rows = []
        for i, c in enumerate(self.closes):
            t = 1000 + i * 60
            rows.append([t, "1", str(c), str(c + 1), str(c - 1), str(c - 0.5), "0"])
        return rows


def _state_cond(**kw):
    c = {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 5,
         "side": "above", "cooldown_sec": 1}
    c.update(kw)
    return c


class TestStateEdgeTrigger(unittest.TestCase):
    def test_sustained_state_fires_once(self):
        """状态持续为真 → 只报一次，冷却过了也不重报。"""
        md, states = MutMD(ASC), {}
        first = check_conditions(md, [_state_cond()], "1m", states=states, now=T0)
        second = check_conditions(md, [_state_cond()], "1m", states=states, now=T0 + 9999)
        self.assertEqual(len(first), 1, "首次为真应报")
        self.assertEqual(len(second), 0, "状态没变，不该重报（这就是边沿）")

    def test_opt_out_restores_timer_semantics(self):
        """`edge_trigger: false` → 恢复旧的「持续成立就按冷却反复报」。"""
        md, states = MutMD(ASC), {}
        cond = _state_cond(edge_trigger=False)
        first = check_conditions(md, [cond], "1m", states=states, now=T0)
        second = check_conditions(md, [cond], "1m", states=states, now=T0 + 9999)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1, "显式关掉边沿后应恢复定时器语义")

    def test_false_then_true_fires(self):
        md, states = MutMD(DESC), {}
        off = check_conditions(md, [_state_cond()], "1m", states=states, now=T0)
        md.closes = ASC
        on = check_conditions(md, [_state_cond()], "1m", states=states, now=T0 + 2000)
        self.assertEqual(len(off), 0, "条件不成立时不该报")
        self.assertEqual(len(on), 1, "False→True 应报")

    def test_rearms_after_flip(self):
        """True → False → True 应再报一次（边沿可重复，不是只报一次）。"""
        md, states = MutMD(ASC), {}
        seq = []
        for closes in (ASC, ASC, DESC, DESC, ASC):
            md.closes = closes
            seq.append(len(check_conditions(md, [_state_cond()], "1m",
                                            states=states, now=T0 + len(seq) * 100)))
        self.assertEqual(seq, [1, 0, 0, 0, 1],
                         "应为 报/不报/不报/不报/再报")

    def test_cooldown_still_applies(self):
        """边沿之外，冷却仍在 —— 极快的 True→False→True 不该绕过冷却。"""
        md, states = MutMD(ASC), {}
        cond = _state_cond(cooldown_sec=3600)
        a = check_conditions(md, [cond], "1m", states=states, now=T0)
        md.closes = DESC
        check_conditions(md, [cond], "1m", states=states, now=T0 + 10)   # 冷却内：不报
        md.closes = ASC
        b = check_conditions(md, [cond], "1m", states=states, now=T0 + 20)  # 仍在冷却内
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 0, "冷却期内即使发生边沿也不该报")

    def test_restart_treats_existing_state_as_new(self):
        """进程重启后状态位清零 → 已成立的状态会立刻报一次（对进程而言是新的）。"""
        md = MutMD(ASC)
        fresh_states = {}          # 模拟重启：states 是空的
        fired = check_conditions(md, [_state_cond()], "1m", states=fresh_states, now=T0)
        self.assertEqual(len(fired), 1)


class TestEventConditionsUnaffected(unittest.TestCase):
    def test_price_break_fires_then_silent(self):
        """事件型：破位那一刻报，之后价格维持在高位也不重报（边沿同样保护它）。"""
        md = MutMD(ASC)
        cond = {"type": "price_break", "symbol": "BTC_USDT", "lookback": 10,
                "side": "high", "cooldown_sec": 1}
        states = {}
        seq = [len(check_conditions(md, [cond], "1m", states=states, now=T0 + i * 100))
               for i in range(3)]
        self.assertEqual(seq[0], 1, "创新高应报")
        self.assertEqual(seq[1:], [0, 0], "价格维持在高位不该每轮重报")


if __name__ == "__main__":
    unittest.main()
