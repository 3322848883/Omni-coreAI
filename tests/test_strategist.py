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

    def test_account_fail_does_not_call_llm(self):
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

        class NeverLLM:
            def chat(self, a, b):
                raise AssertionError("no llm")

        from gate_bot.strategist.loop import PlanRunner, StrategistConfig

        cfg = StrategistConfig(symbols=["BTC_USDT"], candles=3, timeframe="15m")
        runner = PlanRunner(Boom(), cfg, Path("inbox/x"), Path("hist/x"), llm=NeverLLM())
        r = runner.run_once()
        self.assertEqual(r.get("error"), "account_unavailable")


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
