import unittest


from pathlib import Path





import sys


ROOT = Path(__file__).resolve().parents[1]


sys.path.insert(0, str(ROOT))





from gate_bot.strategist.tools import NATIVE_TOOLS, TOOL_NAMES, extract_tool_calls, run_tool  # noqa: E402








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








class FakeKlineClient(FakeClient):


    """Adapter with get_klines 鈥?per-exchange REST source."""





    def __init__(self, name="binance"):


        self.name = name


        self.kline_calls = 0





    def get_klines(self, symbol, interval, limit=100):


        self.kline_calls += 1


        import time


        now = int(time.time())


        rows = []


        px = 100.0


        for i in range(int(limit)):


            px += 0.3 if i % 2 == 0 else -0.2


            t = now - (int(limit) - i) * 900


            rows.append({"t": t, "o": px, "h": px + 1, "l": px - 1, "c": px, "v": 10})


        return rows








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





    def test_smc_tools_registered_for_native_tools(self):


        self.assertIn("smc_map", TOOL_NAMES)


        names = [t["function"]["name"] for t in NATIVE_TOOLS]


        self.assertIn("smc_map", names)


        self.assertIn("smc_events", names)


        self.assertIn("sqzmom", names)


        self.assertEqual(len(TOOL_NAMES), len(NATIVE_TOOLS))





    def test_smc_events_tool(self):


        from gate_bot.strategist.market import MarketConfig





        c = FakeKlineClient("binance")


        out = run_tool(


            c, "smc_events",


            {"symbol": "BTC_USDT", "tf": "15m", "limit": 120},


            env="live",


            market_cfg=MarketConfig(mode="rest_only", exchange="binance"),


        )


        self.assertNotIn("error", out)


        self.assertEqual(out["set"], "events")


        self.assertEqual(out["source"], "exchange")


        self.assertIn(out.get("trend"), ("bull", "bear", "neutral"))


        self.assertIn("order_blocks", out)


        self.assertIn("fvgs", out)


        self.assertIn("sweeps", out)





    def test_sqzmom_tool(self):


        from gate_bot.strategist.market import MarketConfig





        c = FakeKlineClient("okx")


        out = run_tool(


            c, "sqzmom",


            {"symbol": "BTC_USDT", "tf": "15m", "limit": 120},


            env="live",


            market_cfg=MarketConfig(mode="rest_only", exchange="okx"),


        )


        self.assertNotIn("error", out)


        self.assertEqual(out["source"], "exchange")


        self.assertEqual(out["bb_length"], 20)


        self.assertIn("sqz_mom", out)


        self.assertIn("sqz_state", out)


        if out["sqz_state"] is not None:


            self.assertIn(out["sqz_state"], (-1, 0, 1))





    def test_smc_tool_uses_venue_klines(self):


        from gate_bot.strategist.market import MarketConfig





        c = FakeKlineClient("binance")


        out = run_tool(


            c, "smc_map",


            {"symbol": "BTC_USDT", "tf": "15m", "limit": 60},


            env="live",


            market_cfg=MarketConfig(mode="rest_only", exchange="binance"),


        )


        self.assertNotIn("error", out)


        self.assertEqual(c.kline_calls, 1)


        self.assertEqual(out["source"], "exchange")


        self.assertEqual(out["symbol"], "BTC_USDT")


        self.assertEqual(out["tf"], "15m")


        self.assertGreaterEqual(out["n"], 30)


        self.assertIn(out.get("swing_trend"), ("bull", "bear", "neutral"))


        self.assertIn("premium_discount", out)


        self.assertIn("order_blocks", out)





    def test_smc_tool_reports_source_and_n(self):


        from gate_bot.strategist.market import MarketConfig





        c = FakeKlineClient("okx")


        out = run_tool(


            c, "smc_map",


            {"symbol": "BTC_USDT", "tf": "15m", "limit": 40},


            env="live",


            market_cfg=MarketConfig(mode="rest_only", exchange="okx"),


        )


        self.assertEqual(out["source"], "exchange")


        self.assertEqual(out["n"], 40)


        self.assertEqual(c.name, "okx")








if __name__ == "__main__":


    unittest.main()


