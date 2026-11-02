"""paper 条件单视图必须与 Gate `/price_orders` 同构（2026-11-02）。

背景（本地 3 轮真实 plan → `once` → 落地审计发现）：
- Gate `/price_orders` 返回嵌套：`trigger.{price,rule,price_type}` + `initial.size`；
- paper 台账是扁平列（`trigger_price` / `rule` / `size`），`_price_order_view`
  只补了 `id` → executor 里所有按嵌套读的统计**恒为零**；
- `Executor._ensure_ladder_protection` 的 `covered` 因此永远 0 < need →
  每轮 plan 都重挂一条同价 SL（实测 3 轮后多侧 2 条 -13、空侧 2 条），
  本地"SL 重复挂"结论完全不可信（线上走 gate_client 不会出现）。

本测试把「paper 视图能让 covered 统计出正确张数」这条契约锁住。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.paper.engine import _price_order_view  # noqa: E402


def _row(size, rule, trigger, reduce_only=True, price_type=1, status="untriggered"):
    return {
        "order_id": "po-1", "contract": "ETH_USDT", "trigger_price": trigger,
        "trigger_price_type": price_type, "order_type": "market", "price": "0",
        "size": size, "side": "sell" if size < 0 else "buy", "rule": rule,
        "reduce_only": reduce_only, "status": status,
        "text": "t-mtgbp-signal-sl",
    }


def _covered(view: dict, side: str) -> int:
    """复刻 executor `_ensure_ladder_protection` 的 covered 统计口径。"""
    want_rule = 2 if side == "long" else 1
    trig = view.get("trigger") or {}
    if int(trig.get("rule") or 0) != want_rule:
        return 0
    ini = view.get("initial") or {}
    return abs(int(float(ini.get("size") or 0)))


class TestPriceOrderViewShape(unittest.TestCase):
    def test_nested_trigger_and_initial_exist(self):
        v = _price_order_view(_row(-13, 2, 1855.15))
        self.assertEqual(v["trigger"]["rule"], 2)
        self.assertEqual(v["trigger"]["price"], "1855.15")
        self.assertEqual(v["trigger"]["price_type"], 1)
        self.assertEqual(v["initial"]["size"], -13)
        self.assertTrue(v["initial"]["reduce_only"])

    def test_covered_counts_long_sl(self):
        """多单 SL：rule=2、size=-13 → covered 必须是 13，不是 0。"""
        v = _price_order_view(_row(-13, 2, 1855.15))
        self.assertEqual(_covered(v, "long"), 13)

    def test_covered_counts_short_sl(self):
        """空单 SL：rule=1、size=+12 → covered 必须是 12。"""
        v = _price_order_view(_row(12, 1, 2724.1))
        self.assertEqual(_covered(v, "short"), 12)

    def test_covered_zero_when_rule_mismatch(self):
        """多单侧看到空单的 SL（rule=1）不该计入覆盖。"""
        v = _price_order_view(_row(12, 1, 2724.1))
        self.assertEqual(_covered(v, "long"), 0)

    def test_flat_keys_preserved(self):
        """向后兼容：原有扁平键一个都不能少（paper 自身与审计脚本按扁平读）。"""
        v = _price_order_view(_row(-3, 2, 2313.86))
        for k in ("order_id", "id", "trigger_price", "rule", "size",
                  "reduce_only", "status", "text"):
            self.assertIn(k, v)
        self.assertEqual(v["size"], -3)

    def test_bad_values_do_not_raise(self):
        row = _row(-3, 2, 2313.86)
        row["trigger_price"] = None
        row["size"] = "oops"
        v = _price_order_view(row)
        self.assertEqual(v["trigger"]["price"], "")
        self.assertEqual(v["initial"]["size"], 0)

    def test_status_untriggered_maps_to_open(self):
        """paper 的 `untriggered` 必须映射成 Gate 的 `open`。

        否则 executor 里 `status not in ("open","")` 把每条 SL 当成已终结跳过 →
        `covered` 恒 0 → **每轮再挂一条同价 SL**（实测 2 轮后 4 条）。
        """
        v = _price_order_view(_row(-13, 2, 1855.15))
        self.assertEqual(v["status"], "open")

    def test_terminal_status_preserved(self):
        for st in ("cancelled", "filled", "triggered"):
            v = _price_order_view(_row(-13, 2, 1855.15, status=st))
            self.assertEqual(v["status"], st)

    def test_price_type_string_mark_maps_to_1(self):
        """paper 配 `trigger_price_type: mark`（字符串）→ Gate 语义 1(mark)。

        直接 int('mark') 会抛 → 整个 trigger 退化成 rule=0/price=''（实测踩过）。
        """
        v = _price_order_view(_row(-13, 2, 1855.15, price_type="mark"))
        self.assertEqual(v["trigger"]["price_type"], 1)
        self.assertEqual(v["trigger"]["rule"], 2)

    def test_price_type_latest_maps_to_0(self):
        v = _price_order_view(_row(-13, 2, 1855.15, price_type="latest"))
        self.assertEqual(v["trigger"]["price_type"], 0)


if __name__ == "__main__":
    unittest.main()
