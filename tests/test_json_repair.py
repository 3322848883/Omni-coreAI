# -*- coding: utf-8 -*-
"""解析加固（T12，见 docs/compose/spec/symbol-as-parameter.md [S2.4⑨] / D9）。

实测 18% 的轮次因「JSON 少一个括号 / `scenarios` 非对象」**整轮产出归零**。
修法：`_repair_json` 补「按栈补齐未闭合的 `{`/`[`」，且 `scenarios` 非对象只丢弃 + 告警
（它在契约里早已写明「不要写、无消费方」—— 一个无人消费的字段不该杀死整轮）。
"""
from __future__ import annotations

import unittest

from omnialpha.strategist.schema import PlanError, _repair_json, parse_plan_text

# 真实故障样本 1：少一个 `}`（`{`×3 vs `}`×2，`json.loads` 报 "Expecting ',' delimiter"）
MISSING_BRACE = (
    '{"cycle_id":"c1","reasoning":"test","chips":['
    '{"symbol":"BTC_USDT","action":"open_long","confidence":0.9,"size_usd":50},'
    '{"symbol":"ETH_USDT","action":"hold","confidence":0.5}'
)
# 真实故障样本 2：尾逗号
TRAILING_COMMA = (
    '{"cycle_id":"c2","chips":['
    '{"symbol":"BTC_USDT","action":"hold","confidence":0.5,},],}'
)
# 真实故障样本 3：scenarios 非对象
BAD_SCENARIOS = (
    '{"cycle_id":"c3","chips":['
    '{"symbol":"BTC_USDT","action":"hold","confidence":0.5,"scenarios":"x"}]}'
)


class TestRepairJsonClosesBrackets(unittest.TestCase):
    def test_stack_completion(self):
        for raw, want in (
            ('{"a":1', '{"a":1}'),
            ('{"a":[1,2', '{"a":[1,2]}'),
            ('{"a":{"b":[1', '{"a":{"b":[1]}}'),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_repair_json(raw), want)

    def test_balanced_input_untouched(self):
        for ok in ('{"a":1}', '{"a":[1,2]}', '{"a":{"b":[]}}'):
            with self.subTest(ok=ok):
                self.assertEqual(_repair_json(ok), ok)

    def test_braces_inside_strings_ignored(self):
        """字符串里的 `{`/`}` 不算括号（否则会补出多余的闭合符）。"""
        self.assertEqual(_repair_json('{"a":"}{"}'), '{"a":"}{"}')

    def test_trailing_comma_before_completion(self):
        """补括号前可能是 `,` —— 补完必须再清一次尾逗号，否则 `,}` 仍然非法。"""
        self.assertEqual(_repair_json('{"a":[1,'), '{"a":[1]}')


class TestRealFailureSamplesParse(unittest.TestCase):
    """三个真实故障样本都必须解析成功（且语义不变）。"""

    def test_missing_closing_brace(self):
        plan = parse_plan_text(MISSING_BRACE)
        self.assertEqual([c.symbol for c in plan.chips], ["BTC_USDT", "ETH_USDT"])
        self.assertEqual(plan.chips[0].action, "open_long")

    def test_trailing_comma(self):
        plan = parse_plan_text(TRAILING_COMMA)
        self.assertEqual(plan.chips[0].symbol, "BTC_USDT")

    def test_scenarios_non_object_is_discarded_not_fatal(self):
        plan = parse_plan_text(BAD_SCENARIOS)
        self.assertEqual(plan.chips[0].action, "hold")
        self.assertEqual(plan.chips[0].scenarios, {})
        self.assertTrue(plan.notes, "丢弃必须留痕（告警），不能静默")

    def test_scenarios_not_object_keeps_other_chips(self):
        text = ('{"chips":[{"symbol":"BTC_USDT","action":"hold","confidence":0.5,'
                '"scenarios":42},{"symbol":"ETH_USDT","action":"hold","confidence":0.5}]}')
        plan = parse_plan_text(text)
        self.assertEqual([c.symbol for c in plan.chips], ["BTC_USDT", "ETH_USDT"])

    def test_missing_brace_with_markdown_fence(self):
        plan = parse_plan_text("```json\n" + MISSING_BRACE + "\n```")
        self.assertEqual(len(plan.chips), 2)

    def test_still_rejects_unparseable(self):
        for bad in ("not json at all", "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(PlanError):
                    parse_plan_text(bad)

    def test_good_input_unchanged(self):
        text = '{"cycle_id":"ok","chips":[{"symbol":"BTC_USDT","action":"hold","confidence":0.5}]}'
        plan = parse_plan_text(text)
        self.assertEqual(plan.cycle_id, "ok")
        self.assertEqual(plan.notes, [], "正常输入不该留下任何告警")


if __name__ == "__main__":
    unittest.main()
