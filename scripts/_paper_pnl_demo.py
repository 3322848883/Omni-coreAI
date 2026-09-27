"""演示实时盈亏：开仓后价格跳动，看 unrealised/equity 是否实时跟。"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.paper.exchange import PaperExchange  # noqa: E402


class Meta:
    quanto_multiplier = 0.0001
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class Feed:
    def __init__(self):
        self.bid = 100000.0
        self.ask = 100010.0
        self.last = 100005.0
        self.funding_rate = 0.0001

    def get_last_price(self, s):
        return self.last

    def get_orderbook_top(self, s, limit=5):
        return {"bids": [{"p": self.bid, "s": 100}], "asks": [{"p": self.ask, "s": 100}]}

    def get_ticker(self, s):
        return {"last": self.last, "mark_price": self.last, "funding_rate": self.funding_rate}

    def get_contract(self, s):
        return Meta()

    def get_klines(self, *a, **k):
        return []

    def get_contract_stats(self, *a, **k):
        return []


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="paper-pnl-")
    try:
        feed = Feed()
        ex = PaperExchange(
            env="paper",
            store_path=Path(tmp) / "account.db",
            feed=feed,
            config={"initial_capital": 10000, "leverage": 20, "fee_rate": 0.0005},
        )

        print("=== 开仓：1000 张 @ 100005（约 10U 名义/20x）===")
        ex.place_order({"contract": "BTC_USDT", "size": 1000, "price": "0",
                        "tif": "ioc", "text": "t-pnl"})
        acct = ex.get_account()
        print(f"  entry 后: balance={acct['balance']:.4f} unreal={acct['unrealised_pnl']:.4f} "
              f"equity={acct['equity']:.4f}")

        print("\n=== 模拟价格实时跳动（每档刷新估值）===")
        prices = [100005, 100200, 100500, 101000, 100500, 99800, 99500, 102000]
        print(f"  {'时间':<10} {'last':>10} {'unrealised':>12} {'equity':>12} {'available':>12}")
        for i, px in enumerate(prices):
            feed.last = px
            feed.bid, feed.ask = px - 5, px + 5
            snap = ex.engine.recalc_equity()  # 实时估值
            acct = ex.get_account()
            # unrealised = (px - 100005) × 1000 × 0.0001 = (px-100005)×0.1
            expected = (px - 100005.0) * 1000 * 0.0001
            ts = time.strftime("%H:%M:%S")
            print(f"  {ts:<10} {px:>10} {acct['unrealised_pnl']:>12.4f} {acct['equity']:>12.4f} "
                  f"{acct['available']:>12.4f}   expect={expected:.4f}")
            time.sleep(0.3)

        print("\n=== pnl_snapshot 历史（每次估值都落盘）===")
        conn = ex.store._db()
        rows = conn.execute("SELECT snap_time, equity, unrealised, realised FROM pnl_snapshot "
                            "ORDER BY snap_time DESC LIMIT 5").fetchall()
        for r in rows:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(r[0]))}  equity={r[1]:.4f} "
                  f"unreal={r[2]:.4f} realised={r[3]:.4f}")

        print("\n=== account 工具读到的实时字段 ===")
        acct = ex.get_account()
        for k in ("total", "balance", "unrealised_pnl", "equity", "position_margin"):
            print(f"  {k}: {acct.get(k)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
