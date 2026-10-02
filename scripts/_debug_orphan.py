# -*- coding: utf-8 -*-
"""调试 _cleanup_orphan_protectors 每一步判定。"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omnialpha.paper.store import PaperStore  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402

td = Path(tempfile.mkdtemp())
try:
    store = PaperStore(td / "account.db")
    store.init_config({
        "initial_capital": 10000, "leverage": 20, "fee_rate": 0.0005,
        "funding_enabled": False, "position_mode": "single",
        "margin_mode": "isolated", "feed_exchange": "gate",
    })
    store.insert_price_order({
        "contract": "BTC_USDT", "size": -10, "price": 0,
        "text": "t-pt-sl", "reduce_only": 1, "status": "untriggered",
        "trigger_price": 82000.0, "trigger_price_type": "latest",
        "rule": 2, "order_type": "market", "side": "sell",
    })

    class C:
        def get_account(self):
            return {"total": "10000"}

        def get_positions(self):
            return store.get_positions()

        def list_price_orders(self, s=None):
            return store.list_price_orders(s)

        def cancel_price_order(self, pid):
            print("  >> CANCEL CALLED:", pid)
            return store.cancel_price_order(pid)

    ex = Executor(C(), label_prefix="pt", root=td, bot_id="paper-test")
    rows = store.list_price_orders("BTC_USDT")
    print("RAW ROWS:")
    for r in rows:
        print("  ", dict(r))

    print("\nSTEP-BY-STEP:")
    for p in rows:
        init = p.get("initial") or {}
        text = str(init.get("text") or p.get("text") or "")
        tail = text.rsplit("-", 1)[-1].lower()
        ro = ex._order_is_reduce_only(p)
        status = str(p.get("status") or "").lower()
        pid = ex._order_id(p)
        owned = ex._text_owned(text, "pt")
        is_or = ex._is_orphan_protector(p, [])
        print(f"  text={text!r} tail={tail!r} ro={ro} status={status!r}")
        print(f"    pid={pid!r} owned={owned} is_orphan={is_or}")
        # 模拟 cleanup 的逐步检查
        print(f"    check tail: {tail in ('tp','sl','lp','ls')}")
        print(f"    check ro: {bool(ro)}")
        print(f"    check status skip: {status in ('cancelled','finished','filled','triggered','failed','closed')}")
        print(f"    check pid truthy: {bool(pid)}")
        print(f"    check owned: {owned}")

    print("\nCALL CLEANUP:")
    print("result:", ex._cleanup_orphan_protectors("BTC_USDT"))
finally:
    shutil.rmtree(td, ignore_errors=True)
