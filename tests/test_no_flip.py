"""no_flip 闸门：默认拦反向开仓，配 false 后放行（阶梯双向的前提）。

背景：阶梯策略**永远双向**，但 `_check_no_flip` 默认开启（禁止持有 long 时开
short、反之亦然）。线上实测 8 小时里 30 次执行失败全因它 —— 一旦有一侧成交，
另一侧就再也挂不上，双向对冲退化成单边。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import GateApiError  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402


class _Client:
    """只够 `_check_no_flip` 用：返回一个指定方向的持仓。"""

    def __init__(self, size=10):
        self._size = size

    def get_positions(self, contract=None):
        return [{"contract": "ETH_USDT", "size": self._size, "mode": "dual_long",
                 "entry_price": 2600.0, "mark_price": 2600.0}]

    def banner(self):
        return ""


def _ex(root, risk, size=10):
    return Executor(_Client(size), symbols_whitelist=["ETH_USDT"], root=root,
                    bot_id="b1", label_prefix="b1", require_sl=False,
                    account_risk=risk)


def _intent(action):
    return Intent(action=action, symbol="ETH_USDT",
                  side="short" if action.endswith("short") else "long")


class TestNoFlip(unittest.TestCase):
    def test_default_blocks_reverse(self):
        """默认（没配 no_flip）→ 持多时开空被拦。"""
        with tempfile.TemporaryDirectory() as td:
            ex = _ex(Path(td), {})
            with self.assertRaises(GateApiError) as cm:
                ex._check_no_flip(_intent("open_short"))
            self.assertIn("NO_FLIP", str(cm.exception))

    def test_disabled_allows_dual(self):
        """`no_flip: false` → 持多时也能开空。"""
        with tempfile.TemporaryDirectory() as td:
            ex = _ex(Path(td), {"no_flip": False})
            ex._check_no_flip(_intent("open_short"))

    def test_same_side_never_blocked(self):
        """同向加仓不受这个闸门影响。"""
        with tempfile.TemporaryDirectory() as td:
            ex = _ex(Path(td), {})
            ex._check_no_flip(_intent("open_long"))

    def test_flat_never_blocked(self):
        """空仓时两个方向都能开。"""
        with tempfile.TemporaryDirectory() as td:
            ex = _ex(Path(td), {}, size=0)
            ex._check_no_flip(_intent("open_short"))
            ex._check_no_flip(_intent("open_long"))

    def test_stop_entry_also_gated(self):
        """突破单同样受闸门约束（它也是开仓）。"""
        with tempfile.TemporaryDirectory() as td:
            ex = _ex(Path(td), {})
            with self.assertRaises(GateApiError):
                ex._check_no_flip(_intent("stop_entry_short"))


if __name__ == "__main__":
    unittest.main()
