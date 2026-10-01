import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.schema import PlanError, parse_plan, parse_plan_text  # noqa: E402
from gate_bot.strategist.risk import RiskConfig, apply_risk  # noqa: E402
from gate_bot.strategist.bridge import chips_to_signal, write_signal_file  # noqa: E402
from gate_bot.strategist.llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402
from gate_bot.schema import parse_signal  # noqa: E402


class TestPlanSchema(unittest.TestCase):
    def test_parse_plan_ok(self):
        plan = parse_plan(
            {
                "cycle_id": "c1",
                "reasoning": "x",
                "chips": [
                    {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 50, "sl": 1},
                    {"symbol": "ETH_USDT", "action": "hold", "confidence": 0.5},
                ],
            }
        )
        self.assertEqual(len(plan.chips), 2)
        self.assertEqual(plan.chips[0].action, "open_long")

    def test_parse_plan_text_fenced(self):
        plan = parse_plan_text('```json\n{"chips":[{"symbol":"BTC_USDT","action":"hold"}]}\n```')
        self.assertEqual(plan.chips[0].action, "hold")

    def test_parse_plan_bad_action(self):
        with self.assertRaises(PlanError):
            parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "yolo"}]})

    def test_parse_plan_not_json(self):
        with self.assertRaises(PlanError):
            parse_plan_text("not json")

    def test_confidence_scale_100(self):
        plan = parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 85}]})
        self.assertAlmostEqual(plan.chips[0].confidence, 0.85)

    def test_type_field_action_name_recovered(self):
        """Model sometimes writes type=stop_entry_long; recover as action."""
        plan = parse_plan(
            {
                "chips": [
                    {
                        "symbol": "BTC_USDT",
                        "type": "stop_entry_long",
                        "confidence": 0.8,
                        "size_usd": 100,
                        "sl": 1,
                        "trigger_price": 2,
                    }
                ]
            }
        )
        c = plan.chips[0]
        self.assertEqual(c.action, "stop_entry_long")
        self.assertEqual(c.order_type, "market")
        self.assertEqual(c.trigger_price, 2)


class TestRisk(unittest.TestCase):
    def _plan(self):
        return parse_plan(
            {
                "chips": [
                    {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 50},
                    {"symbol": "ETH_USDT", "action": "open_short", "confidence": 0.6, "size_usd": 50},
                    {"symbol": "SOL_USDT", "action": "open_long", "confidence": 0.95, "size_usd": 50},
                    {"symbol": "XRP_USDT", "action": "close", "confidence": 0.99},
                ]
            }
        )

    def test_min_confidence_hold(self):
        r = apply_risk(self._plan(), RiskConfig(min_confidence=0.8, max_chips=10))
        acts = {c.symbol: c.action for c in r.accepted}
        self.assertEqual(acts.get("ETH_USDT"), "hold")

    def test_max_notional_reject(self):
        plan = parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 100}]})
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, max_notional_usd=40))
        self.assertFalse(r.accepted)  # oversized → reject (not silent truncate)
        self.assertTrue(r.rejected)

    def test_allow_actions(self):
        r = apply_risk(self._plan(), RiskConfig(min_confidence=0.5, allow_actions={"hold", "close"}))
        actions = {c.action for c in r.accepted}
        self.assertNotIn("open_long", actions)

    def test_max_chips(self):
        r = apply_risk(self._plan(), RiskConfig(min_confidence=0.5, max_chips=1))
        tradeable = [c for c in r.accepted if c.action != "hold"]
        self.assertLessEqual(len(tradeable), 1)

    def test_open_requires_size(self):
        plan = parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9}]})
        r = apply_risk(plan, RiskConfig(min_confidence=0.5))
        self.assertTrue(all(c.action == "hold" for c in r.accepted))


class TestBridge(unittest.TestCase):
    def test_bridge_to_parse_signal(self):
        plan = parse_plan(
            {
                "cycle_id": "c9",
                "chips": [
                    {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 30, "sl": 1},
                    {"symbol": "ETH_USDT", "action": "hold", "confidence": 0.2},
                ],
            }
        )
        risk = apply_risk(plan, RiskConfig(min_confidence=0.75, max_notional_usd=50))
        payload = chips_to_signal(plan, risk, bot_id="alpha")
        self.assertTrue(payload.get("orders"))
        sig = parse_signal(payload)  # must be executable schema
        self.assertTrue(sig.intents)

    def test_write_hold_when_empty(self):
        plan = parse_plan({"cycle_id": "c0", "chips": [{"symbol": "BTC_USDT", "action": "hold"}]})
        risk = apply_risk(plan, RiskConfig(min_confidence=0.9))
        payload = chips_to_signal(plan, risk)
        with tempfile.TemporaryDirectory() as td:
            p = write_signal_file(Path(td), payload, cycle_id="c0")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data.get("action"), "hold")

    def test_hold_with_tpsl_becomes_modify(self):
        """hold + tp/sl must become modify_tp_sl (was silently dropped)."""
        plan = parse_plan(
            {
                "cycle_id": "m1",
                "chips": [
                    {
                        "symbol": "BTC_USDT",
                        "action": "hold",
                        "confidence": 0.66,
                        "tp": 85150,
                        "sl": 84400,
                    }
                ],
            }
        )
        risk = apply_risk(plan, RiskConfig(min_confidence=0.7))
        payload = chips_to_signal(plan, risk, bot_id="brk")
        self.assertEqual(len(payload.get("orders") or []), 1)
        item = payload["orders"][0]
        self.assertEqual(item["action"], "modify_tp_sl")
        self.assertEqual(item["tp"], 85150)
        self.assertEqual(item["sl"], 84400)
        sig = parse_signal(payload)
        self.assertEqual(sig.intents[0].action, "modify_tp_sl")
        self.assertEqual(sig.intents[0].tp, 85150)
        self.assertEqual(sig.intents[0].sl, 84400)

    def test_hold_tpsl_only_moves_given_leg(self):
        plan = parse_plan(
            {"chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.5, "sl": 84400}]}
        )
        risk = apply_risk(plan, RiskConfig(min_confidence=0.9))
        payload = chips_to_signal(plan, risk)
        item = payload["orders"][0]
        self.assertEqual(item["action"], "modify_tp_sl")
        self.assertEqual(item["sl"], 84400)
        self.assertNotIn("tp", item)

    def test_modify_tp_sl_chip_accepted_low_conf(self):
        plan = parse_plan(
            {
                "chips": [
                    {"symbol": "BTC_USDT", "action": "modify_tp_sl", "confidence": 0.4, "tp": 85000}
                ]
            }
        )
        risk = apply_risk(plan, RiskConfig(min_confidence=0.7))
        self.assertEqual(risk.accepted[0].action, "modify_tp_sl")
        payload = chips_to_signal(plan, risk)
        self.assertEqual(payload["orders"][0]["action"], "modify_tp_sl")

    def test_modify_tp_sl_requires_level(self):
        plan = parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "modify_tp_sl", "confidence": 0.9}]})
        risk = apply_risk(plan, RiskConfig(min_confidence=0.5))
        self.assertTrue(all(c.action != "modify_tp_sl" for c in risk.accepted))
        self.assertTrue(risk.rejected)

    def test_chip_side_and_tpsl_mode_roundtrip(self):
        plan = parse_plan(
            {
                "chips": [
                    {
                        "symbol": "BTC_USDT",
                        "action": "modify_tp_sl",
                        "confidence": 0.9,
                        "sl": 84400,
                        "side": "long",
                        "tp_mode": "limit_order",
                    }
                ]
            }
        )
        chip = plan.chips[0]
        self.assertEqual(chip.side, "long")
        self.assertEqual(chip.tp_mode, "limit_order")
        d = chip.to_signal_dict()
        self.assertEqual(d["side"], "long")
        self.assertEqual(d["tp_mode"], "limit_order")
        sig = parse_signal({"orders": [d]})
        self.assertEqual(sig.intents[0].side, "long")
        self.assertEqual(sig.intents[0].tp_mode, "limit_order")

    def test_chip_side_invalid(self):
        with self.assertRaises(PlanError):
            parse_plan(
                {"chips": [{"symbol": "BTC_USDT", "action": "close", "confidence": 0.9, "side": "up"}]}
            )


class TestLLMClient(unittest.TestCase):
    def test_missing_key(self):
        import os

        os.environ.pop("OPENAI_API_KEY", None)
        client = LLMClient(LLMConfig(api_key_env="OPENAI_API_KEY_MISSING_TEST"))
        with self.assertRaises(LLMError):
            client.chat("s", "u")

    def test_parse_response(self):
        import os
        import urllib.request

        os.environ["OPENAI_API_KEY"] = "k"
        client = LLMClient(LLMConfig(api_key_env="OPENAI_API_KEY"))

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{\"chips\":[]}"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        old = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: FakeResp()
        try:
            self.assertEqual(client.chat("s", "u"), '{"chips":[]}')
        finally:
            urllib.request.urlopen = old
            os.environ.pop("OPENAI_API_KEY", None)


class TestPlanTriggers(unittest.TestCase):
    """plan-loop 定时 / K 线收盘触发与 cycle 去重。"""

    def _runner(self, client, **kw):
        from gate_bot.strategist.loop import PlanRunner, StrategistConfig

        cfg = StrategistConfig(
            symbols=["BTC_USDT"],
            timeframe="15m",
            interval_sec=kw.pop("interval_sec", 300),
            event_on_kline_close=kw.pop("event_on_kline_close", True),
            candles=5,
            write_hold=True,
            **kw,
        )
        return PlanRunner(client, cfg, Path("inbox/x"), Path("hist/x"), llm=kw.pop("llm", None) or _HoldLLM())

    def test_kline_closed_fires_once_per_advance(self):
        class KClient:
            def __init__(self):
                self.ts = 1000

            def public_get(self, path, qs=""):
                return [[self.ts, "1", "1", "1", "1", "1", "0"]]

            def get_account(self):
                return {"available": "1", "total": "1"}

            def get_positions(self):
                return []

        from gate_bot.strategist.loop import PlanRunner, StrategistConfig

        c = KClient()
        runner = self._runner(c)
        # first call only seeds _last_kline_t
        self.assertFalse(runner._kline_closed())
        self.assertFalse(runner._kline_closed())
        c.ts = 2700
        self.assertTrue(runner._kline_closed())
        self.assertFalse(runner._kline_closed())

    def test_duplicate_cycle_skips_orders(self):
        from gate_bot.strategist.loop import PlanRunner, StrategistConfig
        from gate_bot.strategist.schema import parse_plan

        class FixedLLM:
            def __init__(self):
                self.n = 0

            def chat(self, system, user):
                self.n += 1
                return json.dumps({
                    "cycle_id": "same-cycle",
                    "chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.9}],
                })

        class C:
            def public_get(self, path, qs=""):
                return [[1, "1", "1", "1", "1", "1", "0"]]

            def get_last_price(self, symbol):
                return 100.0

            def get_ticker(self, symbol):
                return {"last": "100", "funding_rate": "0.0001", "mark_price": "100"}

            def get_contract_stats(self, symbol, limit=1):
                return [{"open_interest": 1}]

            def get_orderbook_top(self, symbol, limit=5):
                return {"bids": [{"p": "99", "s": "1"}], "asks": [{"p": "101", "s": "1"}]}

            def get_account(self):
                return {"available": "1", "total": "1"}

            def get_positions(self):
                return []

        from gate_bot.strategist.loop import PlanRunner, StrategistConfig

        cfg = StrategistConfig(symbols=["BTC_USDT"], candles=3, timeframe="15m")
        runner = PlanRunner(C(), cfg, Path("inbox/x"), Path("hist/x"), llm=FixedLLM())
        r1 = runner.run_once()
        r2 = runner.run_once()
        self.assertTrue(r1.get("ok"))
        self.assertEqual(r2.get("skipped"), "duplicate_cycle")

    def test_account_fail_still_calls_llm(self):
        """账户取不到不阻断：仍用行情分析（不再返回 account_unavailable）。"""

        class Boom:
            def get_ticker(self, symbol):
                return {"last": "1"}

            def get_last_price(self, s):
                return 1.0

            def get_contract_stats(self, symbol, limit=1):
                return []

            def get_orderbook_top(self, symbol, limit=5):
                return {"bids": [], "asks": []}

            def public_get(self, path, qs=""):
                return []

            def get_account(self):
                raise RuntimeError("down")

            def get_positions(self):
                return []

        called = {"n": 0}

        class StubLLM:
            last_reasoning = ""
            last_reasoning_chain = []

            def chat(self, a, b):
                called["n"] += 1
                return '{"cycle_id":"c","reasoning":"行情分析","chips":[{"symbol":"BTC_USDT","action":"hold","confidence":0.5}]}'

        from gate_bot.strategist.loop import PlanRunner, StrategistConfig

        cfg = StrategistConfig(symbols=["BTC_USDT"], candles=3, timeframe="15m")
        runner = PlanRunner(Boom(), cfg, Path("inbox/x"), Path("hist/x"), llm=StubLLM())
        r = runner.run_once()
        self.assertNotEqual(r.get("error"), "account_unavailable", "账户缺失不应中止分析")
        self.assertGreater(called["n"], 0, "LLM 应仍被调用（行情分析）")


class TestPromptSandbox(unittest.TestCase):
    def test_prompt_limited_to_prompts_dir(self):
        from gate_bot.strategist.prompt import load_strategy_prompt

        root = Path(__file__).resolve().parents[1] / "prompts"
        self.assertIn("策略", load_strategy_prompt("vergex_default.md", prompts_root=root))
        self.assertIn("策略", load_strategy_prompt(root / "vergex_default.md", prompts_root=root))
        for bad in ("C:/Windows/win.ini", "../gate_bot/executor.py", "/etc/passwd"):
            with self.assertRaises(PermissionError):
                load_strategy_prompt(bad, prompts_root=root)

    def test_prompt_resolves_via_bot_root_not_cwd(self):
        """Server/cron: prompt_file must load from bot_root even if cwd is elsewhere."""
        import os
        import tempfile
        from gate_bot.strategist.prompt import load_strategy_prompt

        with tempfile.TemporaryDirectory() as td:
            bot_root = Path(td)
            (bot_root / "prompts").mkdir()
            (bot_root / "prompts" / "my.md").write_text("策略人格X", encoding="utf-8")
            old = Path.cwd()
            try:
                os.chdir(tempfile.gettempdir())
                text = load_strategy_prompt("prompts/my.md", bot_root=bot_root)
                text2 = load_strategy_prompt("my.md", bot_root=bot_root)
            finally:
                os.chdir(old)
        self.assertEqual(text.strip(), "策略人格X")
        self.assertEqual(text2.strip(), "策略人格X")

    def test_llm_plan_cannot_escape_inbox(self):
        from gate_bot.strategist.bridge import write_signal_file
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td)
            p = write_signal_file(inbox, {"orders": [{"action": "hold"}]}, cycle_id="../../evil:../x")
            self.assertEqual(p.parent, inbox.resolve())
            self.assertNotIn("..", p.name.replace("..-", ""))  # no path sep


class _HoldLLM:
    def chat(self, system, user):
        return json.dumps({"chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.9}]})


class TestTradeLogger(unittest.TestCase):
    def test_write_and_tail(self):
        from gate_bot.tradelog import TradeLogger

        with tempfile.TemporaryDirectory() as td:
            log = TradeLogger(Path(td) / "t.jsonl")
            log.log_execution("alpha", {"signal_id": "s1"}, {"ok": True, "steps": []})
            log.log_plan("alpha", {"cycle_id": "c1", "orders": 1})
            rows = log.tail(10)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["type"], "execution")
            self.assertEqual(rows[1]["type"], "plan")
            self.assertEqual(rows[0]["bot_id"], "alpha")


if __name__ == "__main__":
    unittest.main()
