# -*- coding: utf-8 -*-
"""突破单 stop_entry 专项 e2e。"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor
from omnialpha.paper.exchange import PaperExchange
from omnialpha.schema import parse_signal


class FakeMeta:
    quanto_multiplier = 0.0001
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 8.0


class FakeFeed:
    name = "fake"

    def __init__(self, price=83000.0):
        self.price = price

    def set_price(self, p):
        self.price = p

    def get_last_price(self, symbol):
        return self.price

    def get_orderbook_top(self, symbol, limit=5):
        p = self.price
        return {"bids": [{"p": p - 5, "s": 50}], "asks": [{"p": p + 5, "s": 50}]}

    def get_ticker(self, symbol):
        return {"last": self.price, "mark_price": self.price, "funding_rate": 0}

    def get_contract(self, symbol):
        return FakeMeta()

    def get_klines(self, *a, **k):
        return []

    def get_contract_stats(self, *a, **k):
        return []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    return cond


def main():
    ok = True
    td = tempfile.mkdtemp(prefix="stop-e2e-")
    feed = FakeFeed(83000.0)
    client = PaperExchange(
        env="paper", api_key="", api_secret="",
        store_path=Path(td) / "account.db", feed=feed,
        config={
            "initial_capital": 10000, "leverage": 20, "fee_rate": 0.0005,
            "funding_enabled": False, "position_mode": "single", "margin_mode": "isolated",
            "price_band_pct": 5.0, "maintenance_margin_rate": 0.005,
            "trigger_price_type": "latest", "feed_exchange": "fake",
        },
    )
    ex = Executor(
        client, symbols_whitelist=["BTC_USDT"], max_notional_usd=50000,
        require_sl=True, label_prefix="se", account_risk={"max_notional_pct": 20},
    )

    # A) 买入突破：trigger 83500，未到价不应成交
    r = ex.execute_signal(parse_signal({
        "action": "stop_entry_long", "symbol": "BTC_USDT", "trigger_price": 83500,
        "size_usd": 2000, "tp": 84200, "sl": 83100, "leverage": 20,
    }))
    ok &= check("A stop_entry_long placed", r.ok, str(r.results[0].error or "")[:80])
    pos = [p for p in (client.get_positions() or []) if abs(float(p.get("size") or 0)) > 0]
    ok &= check("A not filled below trigger", not pos, str(pos)[:80])

    # 价仍未触及 → tick 后不应触发
    client.tick() if hasattr(client, "tick") else None
    pos = [p for p in (client.get_positions() or []) if abs(float(p.get("size") or 0)) > 0]
    ok &= check("A still flat after tick @83000", not pos)

    # B) 价上破触发价 → 应触发成交
    feed.set_price(83520.0)
    tinfo = client.tick() if hasattr(client, "tick") else {}
    raw = client.get_positions()
    pos = [p for p in (raw or []) if abs(float(p.get("size") or 0)) > 0]
    print("   debug B tick:", tinfo, "raw_pos:", raw)
    ok &= check("B filled after breakout", len(pos) == 1 and float(pos[0]["size"]) > 0,
                str(pos)[:120])

    # C) 卖出突破（先平多）
    r = ex.execute_signal(parse_signal({
        "action": "close", "symbol": "BTC_USDT", "side": "long",
    }))
    ok &= check("C close", r.ok)

    r = ex.execute_signal(parse_signal({
        "action": "stop_entry_short", "symbol": "BTC_USDT", "trigger_price": 82000,
        "size_usd": 1500, "tp": 81200, "tp2": 80800, "tp1_share": 0.5, "sl": 82500,
        "leverage": 20,
    }))
    ok &= check("C stop_entry_short placed", r.ok)
    pos = [p for p in (client.get_positions() or []) if abs(float(p.get("size") or 0)) > 0]
    ok &= check("C not filled above trigger", not pos)

    feed.set_price(81980.0)
    if hasattr(client, "tick"):
        client.tick()
    pos = [p for p in (client.get_positions() or []) if abs(float(p.get("size") or 0)) > 0]
    ok &= check("C short filled on breakdown", len(pos) == 1 and float(pos[0]["size"]) < 0,
                str(pos)[:100])

    # D) 触发后应带保护
    r = ex.execute_signal(parse_signal({
        "action": "modify_tp_sl", "symbol": "BTC_USDT", "sl": 82200,
    }))
    ok &= check("D modify after fill", r.ok)

    # E) 假突破：触发价远离，不成交
    r = ex.execute_signal(parse_signal({
        "action": "close", "symbol": "BTC_USDT", "side": "short",
    }))
    r = ex.execute_signal(parse_signal({
        "action": "stop_entry_long", "symbol": "BTC_USDT", "trigger_price": 90000,
        "size_usd": 1000, "tp": 91000, "sl": 89500, "leverage": 20,
    }))
    feed.set_price(83000.0)
    if hasattr(client, "tick"):
        client.tick()
    pos = [p for p in (client.get_positions() or []) if abs(float(p.get("size") or 0)) > 0]
    ok &= check("E far trigger no fill", not pos)

    print("\n====", "STOP ENTRY ALL OK" if ok else "STOP ENTRY HAS FAILS")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
