"""模拟盘全面测试：自动进场/离场/盈亏/强平/费率/订单类型。"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.paper.engine import PaperEngine  # noqa: E402
from omnialpha.paper.exchange import PaperExchange  # noqa: E402
from omnialpha.paper.risk import RiskEngine  # noqa: E402
from omnialpha.paper.store import PaperStore  # noqa: E402

PASS, FAIL = 0, 0
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((name, cond, detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}  {detail}")


class Meta:
    quanto_multiplier = 0.0001
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class Feed:
    """可控盘口/最新价/费率的假 feed。"""

    def __init__(self):
        self.bid = 100000.0
        self.ask = 100010.0
        self.last = 100005.0
        self.mark = 100005.0
        self.funding_rate = 0.0001

    def get_last_price(self, s):
        return self.last

    def get_orderbook_top(self, s, limit=5):
        return {"bids": [{"p": self.bid, "s": 100}], "asks": [{"p": self.ask, "s": 100}]}

    def get_ticker(self, s):
        return {"last": self.last, "mark_price": self.mark, "funding_rate": self.funding_rate}

    def get_contract(self, s):
        return Meta()

    def get_klines(self, *a, **k):
        return []

    def get_contract_stats(self, *a, **k):
        return []


def make(tmp: str):
    feed = Feed()
    ex = PaperExchange(
        env="paper",
        store_path=Path(tmp) / "account.db",
        feed=feed,
        config={"initial_capital": 10000, "leverage": 20,
                "fee_rate": 0.0005, "funding_enabled": True,
                "maintenance_margin_rate": 0.005},
    )
    return feed, ex


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="paper-e2e-")
    try:
        print("=" * 64)
        print("A. 自动进场")
        print("=" * 64)
        feed, ex = make(tmp)

        # A1 市价进场
        feed.bid, feed.ask, feed.last = 100000.0, 100010.0, 100005.0
        o = ex.place_order({"contract": "BTC_USDT", "size": 1000, "price": "0", "tif": "ioc",
                            "text": "t-a-market"})
        check("A1 市价单成交于 ask", o["status"] == "filled" and abs(o["avg_price"] - 100010.0) < 0.01,
              f"avg={o['avg_price']}")
        pos = ex.get_positions()
        check("A2 持仓建立", len(pos) == 1 and pos[0]["size"] == 1000, f"pos={pos[0]['size'] if pos else None}")
        check("A3 强平价已算", pos and pos[0].get("liquidation_price") and pos[0]["liquidation_price"] < 100005,
              f"liq={pos[0].get('liquidation_price') if pos else None}")

        # A4 限价单挂单不成交
        o2 = ex.place_order({"contract": "BTC_USDT", "size": 100, "type": "limit",
                             "price": 99000.0, "tif": "gtc", "text": "t-a-limit"})
        check("A4 限价单未到价保持 open", o2["status"] == "open", f"status={o2['status']}")

        # A5 价格跌到限价 → 自动成交
        feed.bid, feed.ask, feed.last = 98990.0, 98995.0, 98992.0
        info = ex.tick()
        o2b = ex.get_order(o2["id"])
        check("A5 限价单触价自动成交", o2b["status"] == "filled", f"status={o2b['status']} avg={o2b.get('avg_price')}")

        print()
        print("=" * 64)
        print("B. 自动离场（TP 触发）")
        print("=" * 64)
        # 挂 TP（卖出平多，rule=1 上穿）
        feed.bid, feed.ask, feed.last = 100000.0, 100010.0, 100005.0
        tp = ex.place_price_order({
            "initial": {"contract": "BTC_USDT", "size": -1100, "price": "0", "tif": "ioc",
                        "reduce_only": True, "text": "t-b-tp"},
            "trigger": {"rule": 1, "price_type": 0, "price": "110000.0"},
        })
        check("B1 TP 触发单挂上", tp.get("status") == "untriggered" and float(tp["trigger_price"]) == 110000.0,
              f"tp={tp.get('trigger_price')}")
        info = ex.tick()
        check("B2 未触价不触发", info["triggered"] == 0, f"triggered={info['triggered']}")
        # 价格涨过 TP → 触发平仓
        feed.bid, feed.ask, feed.last, feed.mark = 110010.0, 110020.0, 110015.0, 110015.0
        info = ex.tick()
        check("B3 TP 触发", info["triggered"] == 1, f"triggered={info['triggered']}")
        pos = ex.get_positions()
        check("B4 TP 平仓后无持仓", len(pos) == 0, f"pos={len(pos)}")
        fills = ex.store.list_fills(limit=20)
        sell = [f for f in fills if f["side"] == "sell"]
        check("B5 TP 平仓成交记录", len(sell) >= 1 and sell[0]["realised_pnl"] != 0,
              f"realised={sell[0]['realised_pnl'] if sell else None}")

        print()
        print("=" * 64)
        print("C. 自动离场（SL 触发）")
        print("=" * 64)
        feed.bid, feed.ask, feed.last = 100000.0, 100010.0, 100005.0
        ex.place_order({"contract": "BTC_USDT", "size": 1000, "price": "0", "tif": "ioc", "text": "t-c-long"})
        sl = ex.place_price_order({
            "initial": {"contract": "BTC_USDT", "size": -1000, "price": "0", "tif": "ioc",
                        "reduce_only": True, "text": "t-c-sl"},
            "trigger": {"rule": 2, "price_type": 0, "price": "90000.0"},
        })
        check("C1 SL 触发单挂上", sl.get("status") == "untriggered", f"sl={sl.get('trigger_price')}")
        feed.bid, feed.ask, feed.last, feed.mark = 89990.0, 89995.0, 89992.0, 89992.0
        info = ex.tick()
        check("C2 SL 触发", info["triggered"] == 1, f"triggered={info['triggered']}")
        pos = ex.get_positions()
        check("C3 SL 平仓后无持仓", len(pos) == 0, f"pos={len(pos)}")
        fills = ex.store.list_fills(limit=30)
        last_sell = [f for f in fills if f["side"] == "sell"][0]
        check("C4 SL 平仓有已实现亏损", last_sell["realised_pnl"] < 0,
              f"realised={last_sell['realised_pnl']}")

        print()
        print("=" * 64)
        print("D. 强平引擎")
        print("=" * 64)
        feed.bid, feed.ask, feed.last = 100000.0, 100010.0, 100005.0
        ex.place_order({"contract": "BTC_USDT", "size": 5000, "price": "0", "tif": "ioc", "text": "t-d-big"})
        pos = ex.get_positions()
        liq = pos[0]["liquidation_price"] if pos else None
        check("D1 强平价存在", liq is not None, f"liq={liq}")
        if liq:
            feed.bid, feed.ask, feed.last, feed.mark = liq - 50, liq - 40, liq - 45, liq - 45
            info = ex.tick()
            check("D2 触及强平价自动强平", len(info["liquidations"]) == 1, f"liq={info['liquidations']}")
            check("D3 强平后无持仓", len(ex.get_positions()) == 0)

        print()
        print("=" * 64)
        print("E. 盈亏/手续费/费率")
        print("=" * 64)
        feed.bid, feed.ask, feed.last = 100000.0, 100000.0, 100000.0
        ex.place_order({"contract": "BTC_USDT", "size": 1000, "price": "0", "tif": "ioc", "text": "t-e-long"})
        feed.bid, feed.ask, feed.last = 110000.0, 110000.0, 110000.0
        snap = ex.engine.recalc_equity()
        # 未实现 = (110000-100000)*1000*0.0001 = 1000
        check("E1 未实现盈亏含 quanto", abs(snap["unrealised"] - 1000.0) < 0.01, f"unreal={snap['unrealised']}")
        bal_before = ex.store.get_account()["balance"]
        ex.risk.settle_funding(force=True)
        bal_after = ex.store.get_account()["balance"]
        check("E2 资金费率入账", abs(bal_after - bal_before) > 0, f"delta={bal_after - bal_before}")
        fees = ex.store.total_fees()
        check("E3 手续费已扣", fees > 0, f"fees={fees}")
        n_before = len(ex.store.list_fills(limit=200))
        ex.place_order({"contract": "BTC_USDT", "size": -1000, "price": "0", "tif": "ioc", "text": "t-e-close"})
        new_fills = ex.store.list_fills(limit=200)[: len(ex.store.list_fills(limit=200)) - n_before + 1]
        realised = sum(f["realised_pnl"] for f in new_fills if f["kind"] == "trade" and f["side"] == "sell")
        check("E4 已实现盈亏含 quanto", abs(realised - 1000.0) < 1.0, f"realised={realised}")

        print()
        print("=" * 64)
        print("F. 订单类型（IOC/FOK/PO）")
        print("=" * 64)
        feed.bid, feed.ask, feed.last = 100000.0, 100010.0, 100005.0
        # IOC 限价不成交 → 立即撤
        o = ex.place_order({"contract": "BTC_USDT", "size": 100, "type": "limit",
                            "price": 98000.0, "tif": "ioc", "text": "t-f-ioc"})
        check("F1 IOC 未成交即撤销", o["status"] == "cancelled", f"status={o['status']}")
        # PO 吃单 → 拒绝
        try:
            ex.place_order({"contract": "BTC_USDT", "size": 100, "type": "limit",
                            "price": 100050.0, "tif": "poc", "text": "t-f-po"})
            check("F2 PO 吃单被拒", False, "should reject")
        except Exception as e:
            check("F2 PO 吃单被拒", "post only" in str(e).lower(), f"err={e}")
        # PO 挂单 → 保留
        o = ex.place_order({"contract": "BTC_USDT", "size": 100, "type": "limit",
                            "price": 99500.0, "tif": "poc", "text": "t-f-po2"})
        check("F3 PO 挂单保留", o["status"] == "open", f"status={o['status']}")

        print()
        print("=" * 64)
        print("G. 突破进场（stop_entry 触发单）")
        print("=" * 64)
        feed.bid, feed.ask, feed.last = 100000.0, 100010.0, 100005.0
        se = ex.place_price_order({
            "initial": {"contract": "BTC_USDT", "size": 1000, "price": "0", "tif": "ioc",
                        "text": "t-g-stop"},
            "trigger": {"rule": 1, "price_type": 0, "price": "105000.0"},
        })
        check("G1 突破单挂上", se.get("status") == "untriggered", f"trigger={se.get('trigger_price')}")
        feed.bid, feed.ask, feed.last = 105010.0, 105020.0, 105015.0
        info = ex.tick()
        check("G2 突破触发进场", info["triggered"] == 1 and len(ex.get_positions()) == 1,
              f"triggered={info['triggered']} pos={len(ex.get_positions())}")

        print()
        print("=" * 64)
        print("H. 账户字段对齐（Gate 形状）")
        print("=" * 64)
        acct = ex.get_account()
        for k in ("available", "total", "balance", "unrealised_pnl", "position_margin",
                  "position_mode", "equity", "realised_pnl"):
            check(f"H1 字段 {k}", k in acct, f"val={acct.get(k)}")
        check("H2 订单含 id 字段", all("id" in o for o in ex.list_orders()))
        check("H3 触发单含 id 字段", all("id" in p for p in ex.list_price_orders()))

        print()
        print("=" * 64)
        print(f"结果: {PASS} PASS / {FAIL} FAIL")
        print("=" * 64)
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  FAILED: {name}  {detail}")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
