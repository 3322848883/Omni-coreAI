import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.tools import extract_tool_calls, run_tool  # noqa: E402


class FakeClient:
    def get_ticker(self, sym):
        return {"last": "100", "mark_price": "100", "funding_rate": "0.0001"}

    def get_contract(self, sym):
        from gate_bot.gate_client import ContractMeta
        return ContractMeta(sym, 0.01, 1, 0.01, 50)

    def get_orderbook_top(self, sym, limit=5):
        return {"bids": [{"p": "99", "s": "1"}], "asks": [{"p": "101", "s": "1"}]}

    def get_contract_stats(self, sym, limit=1):
        return [{"open_interest": 1}]

    def get_account(self):
        return {"available": "10", "total": "10", "position_mode": "single"}

    def get_positions(self):
        return []


class TestTools(unittest.TestCase):
    def test_extract_tool_calls_plain_json(self):
        text = '{"tool_calls":[{"tool":"klines","args":{"symbol":"ETH_USDT","tf":"4h","limit":20}}]}'
        calls = extract_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "klines")

    def test_extract_tool_calls_fenced(self):
        text = '```json\n{"tool_calls":[{"tool":"ticker","args":{"symbol":"ETH_USDT"}}]}\n```'
        calls = extract_tool_calls(text)
        self.assertEqual(calls[0]["tool"], "ticker")

    def test_extract_plan_has_no_tools(self):
        text = '{"cycle_id":"x","chips":[{"symbol":"ETH_USDT","action":"hold"}]}'
        self.assertEqual(extract_tool_calls(text), [])

    def test_run_tool_ticker_and_contract(self):
        c = FakeClient()
        t = run_tool(c, "ticker", {"symbol": "ETH_USDT"})
        self.assertEqual(t["last"], 100.0)
        cm = run_tool(c, "contract", {"symbol": "ETH_USDT"})
        self.assertEqual(cm["quanto_multiplier"], 0.01)

    def test_run_tool_unknown(self):
        r = run_tool(FakeClient(), "evil", {})
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
