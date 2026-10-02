# -*- coding: utf-8 -*-
"""真实 paper 引擎（PaperStore）孤儿清理集成测试。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omnialpha.paper.store import PaperStore  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402


class PaperClient:
    """对齐 PaperExchange.cancel_price_order 的真实语义。"""

    def __init__(self, store):
        self.store = store
        self.cancelled_ids = []

    def get_account(self):
        return {"total": "10000", "available": "10000"}

    def get_positions(self):
        return self.store.get_positions()

    def list_price_orders(self, symbol=None):
        rows = self.store.list_price_orders(symbol)
        return [r for r in rows
                if str(r.get("status") or "") not in ("cancelled", "filled", "rejected", "finished")]

    def cancel_price_order(self, pid):
        po = self.store.get_price_order(str(pid))
        if not po:
            raise RuntimeError(f"price order not found: {pid}")
        self.store.update_price_order(str(pid), status="cancelled", finish_time=int(time.time()))
        self.cancelled_ids.append(str(pid))
        return {"id": str(pid), "status": "cancelled"}


def main() -> None:
    td = Path(tempfile.mkdtemp(prefix="orphan_paper_"))
    try:
        store = PaperStore(td / "account.db")
        store.init_config({
            "initial_capital": 10000, "leverage": 20, "fee_rate": 0.0005,
            "funding_enabled": False, "position_mode": "single",
            "margin_mode": "isolated", "feed_exchange": "gate",
        })
        client = PaperClient(store)
        ex = Executor(client, label_prefix="pt", root=td, bot_id="paper-test")

        print("=== 1) 塞两张孤儿保护单 ===")
        for text, tp, rule in [("t-pt-sl", 82000.0, 2), ("t-pt-tp", 84000.0, 1)]:
            store.insert_price_order({
                "contract": "BTC_USDT", "size": -10, "price": 0,
                "text": text, "reduce_only": 1, "status": "untriggered",
                "trigger_price": tp, "trigger_price_type": "latest",
                "rule": rule, "order_type": "market", "side": "sell",
            })
        orders = client.list_price_orders("BTC_USDT")
        print(f"  挂单数: {len(orders)}")
        for o in orders:
            print(f"   {o.get('text')} reduce_only={o.get('reduce_only')} size={o.get('size')}")
        assert len(orders) == 2

        print("=== 2) 孤儿检测（无持仓）===")
        positions = client.get_positions() or []
        for o in orders:
            is_or = ex._is_orphan_protector(o, positions)
            ro = ex._order_is_reduce_only(o)
            print(f"   {o.get('text')}: reduce_only={ro} orphan={is_or}")
            assert ro, "应识别为 reduce_only"
            assert is_or, "应是孤儿"

        print("=== 3) 执行清理 ===")
        cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
        print(f"  已撤: {cleaned}")
        assert len(cleaned) == 2, f"应撤 2 张，实际 {cleaned}"

        print("=== 4) 清理后 ===")
        remain = client.list_price_orders("BTC_USDT")
        print(f"  剩余挂单: {remain}")
        assert remain == [], "应清空"

        print("=== 5) 有仓时保护单保留 ===")
        store.update_position("BTC_USDT", size=8, entry_price=83000) if hasattr(store, "update_position") else None
        store.insert_price_order({
            "contract": "BTC_USDT", "size": -8, "price": 0,
            "text": "t-pt-sl", "reduce_only": 1, "status": "untriggered",
            "trigger_price": 82000.0, "trigger_price_type": "latest",
            "rule": 2, "order_type": "market", "side": "sell",
        })
        pos = client.get_positions()
        print(f"  持仓: {pos}")
        if pos:
            cleaned2 = ex._cleanup_orphan_protectors("BTC_USDT")
            print(f"  有仓时清理结果: {cleaned2}（应为空）")
            assert cleaned2 == [], "有仓时不应清保护单"

        print()
        print("PASS: 真实 paper 引擎孤儿清理全部 OK")
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    main()
