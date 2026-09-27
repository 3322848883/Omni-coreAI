import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from gate_bot.exchanges import create_exchange, list_exchanges
from gate_bot.strategist.market import MarketConfig, resolve_db_path

EX = list_exchanges()
SYM = "BTC_USDT"
ok_all = True


def check(name, fn):
    global ok_all
    try:
        r = fn()
        print(f"  PASS {name}: {str(r)[:100]}")
        return True
    except Exception as e:
        ok_all = False
        print(f"  FAIL {name}: {type(e).__name__} {str(e)[:120]}")
        return False


print("=== 1. 工厂注册 ===")
print("exchanges:", EX)
assert len(EX) == 6

print("=== 2. 六所公共行情 live ===")
for n in EX:
    print(n)
    c = create_exchange(n, env="live")
    check("last", lambda: c.get_last_price(SYM))
    check("ticker", lambda: (c.get_ticker(SYM) or {}).get("last"))
    check("klines", lambda: len(c.get_klines(SYM, "15m", 5)) >= 1)
    check("book", lambda: len(c.get_orderbook_top(SYM, 2).get("bids") or []) >= 1)
    check("contract", lambda: c.get_contract(SYM).quanto_multiplier)

print("=== 3. 符号映射 ===")
from gate_bot.exchanges.okx import OkxMapper
from gate_bot.exchanges.bybit import BybitExchange
from gate_bot.exchanges.bitget import BitgetMapper
from gate_bot.exchanges.hyperliquid import HyperliquidMapper
from gate_bot.exchanges.binance import BinanceExchange

assert BinanceExchange().mapper.native("BTC_USDT") == "BTCUSDT"
assert OkxMapper().native("BTC_USDT") == "BTC-USDT-SWAP"
assert HyperliquidMapper().native("BTC_USDT") == "BTC"
assert BitgetMapper().native("ETH_USDT") == "ETHUSDT"
print("  PASS mappers")

print("=== 4. TP/SL 方向（+size=买平空） ===")
for n in EX:
    c = create_exchange(n, env="testnet", api_key="k", api_secret="s")
    if not getattr(c, "supports_price_orders", True):
        print("  SKIP", n)
        continue
    captured = {}

    def fake(*a, **k):
        # capture whatever we can
        for x in a:
            if isinstance(x, dict):
                captured.update(x)
        for k2, v2 in (k or {}).items():
            if isinstance(v2, dict):
                captured.update(v2)
        return {"id": 1, "order": {}}

    try:
        if hasattr(c, "_sig"):
            c._sig = fake
        if hasattr(c, "_req"):
            c._req = fake
        if hasattr(c, "_post"):
            c._post = fake
        c.place_price_order({"contract": SYM, "trigger": {"price": 999, "rule": 2},
                             "initial": {"size": 7}})
        side = captured.get("side") or captured.get("is_buy")
        good = side in ("BUY", "Buy", "buy", True)
        print(f"  {'PASS' if good else 'FAIL'} {n} side={side}")
        if not good:
            ok_all = False
    except Exception as e:
        # HL may raise unsupported
        if "unsupported" in str(e).lower() or "signature" in str(e).lower():
            print(f"  SKIP {n} trading-sign: {str(e)[:60]}")
        else:
            ok_all = False
            print(f"  FAIL {n} {str(e)[:80]}")

print("=== 5. db 路径（每所一库） ===")
for n in EX:
    cfg = MarketConfig(mode="rest_only", exchange=n)
    p = resolve_db_path("testnet", cfg, Path("."))
    print(" ", n, "->", p.name)
    if n != "gate" and f"kline_{n}_testnet.db" not in p.name:
        ok_all = False
        print("  FAIL name")

print("=== 6. 账户接口（只读） ===")
for n in EX:
    try:
        c = create_exchange(n, env="live")
        acc = c.get_account() or {}
        print(f"  PASS {n} account keys={list(acc.keys())[:4]}")
    except Exception as e:
        print(f"  WARN {n} account: {str(e)[:80]}")

print()
print("RESULT:", "ALL_OK" if ok_all else "HAS_FAIL")
