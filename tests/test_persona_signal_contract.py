# -*- coding: utf-8 -*-
"""persona 产出的信号必须能被**真实 executor schema** 接受。

这一条是端到端契约：`PersonaRunner._execute` 拼出来的 payload 要直接丢进
`omnialpha.schema.parse_signal`（executor 用的那一个）而不报错。

线上实测踩到的两个反面例子：
- `trigger_price` 被丢 → `stop_entry_short requires trigger_price`（人格的突破单从没挂出去过）
- 讨论环节把模型写的 `stop_market` 原样塞进 type → `unsupported type: 'stop_market'`
  （`_discussion_chip` 绕过了 `parse_plan` 的校验）
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from omnialpha.persona.orders import SharedOrderStore
from omnialpha.persona.runner import PersonaRunner
from omnialpha.schema import parse_signal
from omnialpha.strategist.loop import PlanRunner
from omnialpha.strategist.schema import normalize_chip_type

BOT = "pt-b1"
OID = "o-contract1"


def _runner(root: Path) -> PersonaRunner:
    r = PersonaRunner.__new__(PersonaRunner)
    r.root = root
    r.group = SimpleNamespace(members=[BOT], name="g", topology="single_account",
                              target_account=BOT, fusion_config={})
    r.orders = SharedOrderStore(root)
    r.plan_runners = {}
    r.bots = {}
    r.discussion_log = []
    r.log_path = root / "data" / "shared" / "persona_log.jsonl"
    r.log_path.parent.mkdir(parents=True, exist_ok=True)
    return r


def _capture_execute(r: PersonaRunner, fusion: dict, plans: dict) -> dict:
    captured: dict = {}
    r._exec_single = lambda payload, oid: (captured.update(payload) or {"executed": True})
    r.orders.create({
        "order_id": OID, "symbol": "BTC_USDT", "side": "long",
        "members": [BOT], "target_account": BOT, "status": "open",
    })
    r._execute(fusion, plans, OID)
    return captured


class TestPersonaSignalPassesExecutorSchema(unittest.TestCase):
    def test_stop_entry_signal_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(Path(td))
            fusion = {"action": "stop_entry_long", "decision": "long",
                      "mode": "weighted_vote", "reason": "x", "votes": {BOT: "long"}}
            plans = {BOT: {"chips": [{
                "symbol": "BTC_USDT", "action": "stop_entry_long", "size_usd": 100,
                "trigger_price": 84990, "tp": 85250, "sl": 84730, "type": "market",
            }]}}
            payload = _capture_execute(r, fusion, plans)
            self.assertEqual(payload.get("trigger_price"), 84990,
                             f"trigger_price 没透传：{payload}")
            parse_signal(payload)          # 不抛即通过（executor 用的同一个 schema）

    def test_open_signal_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(Path(td))
            fusion = {"action": "open_long", "decision": "long", "mode": "weighted_vote",
                      "reason": "x", "votes": {BOT: "long"}}
            plans = {BOT: {"chips": [{"symbol": "BTC_USDT", "action": "open_long",
                                      "size_usd": 100, "tp": 86000, "sl": 84000,
                                      "type": "market"}]}}
            parse_signal(_capture_execute(r, fusion, plans))


class TestRiskFieldsNotLeakedFromPlan(unittest.TestCase):
    """机制字段可以透传，**风险字段不行** —— yaml 风控只钳 `size_usd`。

    实测：模型提了 `leverage: 50`（配置是 20），而 pt-b1 没配
    `account_risk.max_leverage`，那个闸门不生效 → 透传就等于让 Plan 越过 yaml 风控。
    """

    def test_leverage_and_size_are_not_passed_through(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(Path(td))
            fusion = {"action": "open_long", "decision": "long", "mode": "weighted_vote",
                      "reason": "x", "votes": {BOT: "long"}}
            plans = {BOT: {"chips": [{"symbol": "BTC_USDT", "action": "open_long",
                                      "size_usd": 100, "tp": 86000, "sl": 84000,
                                      "type": "market", "leverage": 50, "size": 999}]}}
            payload = _capture_execute(r, fusion, plans)
            self.assertNotIn("leverage", payload, f"Plan 的杠杆不该透传：{payload}")
            self.assertNotIn("size", payload, f"Plan 的张数不该透传（会绕过 size_usd 钳制）：{payload}")


class TestDiscussionChipTypeIsNormalized(unittest.TestCase):
    """讨论环节绕过 `parse_plan`，它的 type 必须自己归一。"""

    def test_stop_market_is_normalized(self):
        chip = PlanRunner._discussion_chip(
            {"type": "stop_market", "sl": 84730, "trigger_price": 84990},
            "stop_entry_long", {"chips": [{"symbol": "BTC_USDT"}]},
        )
        self.assertIsNotNone(chip)
        self.assertEqual(chip["type"], "market", f"stop_market 没被归一：{chip}")
        parse_signal({**chip, "action": "stop_entry_long", "size_usd": 100})   # 必须被接受

    def test_unknown_type_falls_back_to_market(self):
        # T3 起 `_discussion_chip` 要求 chip 带合法 symbol（缺了/越界会拒绝），
        # 所以这里给一个宇宙内的标的 —— 本用例只测 type 归一。
        chip = PlanRunner._discussion_chip(
            {"type": "weird_thing", "sl": 1, "trigger_price": 2},
            "stop_entry_long", {"chips": [{"symbol": "ETH_USDT"}]},
            allowed=["ETH_USDT"],
        )
        self.assertEqual(chip["type"], "market")

    def test_alias_table(self):
        self.assertEqual(normalize_chip_type("stop_market"), "market")
        self.assertEqual(normalize_chip_type("stop_limit"), "limit")
        self.assertEqual(normalize_chip_type("STOP_MARKET"), "market")
        self.assertEqual(normalize_chip_type("limit"), "limit")
        self.assertIsNone(normalize_chip_type("nonsense"))


if __name__ == "__main__":
    unittest.main()
