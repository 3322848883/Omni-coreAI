# -*- coding: utf-8 -*-
"""保护单清理矩阵：单级/多级止盈 × 有仓/无仓 × 单/多孤儿。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor

PASS, FAIL = [], []


class FakeClient:
    def __init__(self, price_orders=None, positions=None, label="brk"):
        self.price_orders = price_orders or []
        self.positions = positions or []
        self.cancelled = []
        self.label = label

    def list_price_orders(self, contract=None):
        out = []
        for i, p in enumerate(self.price_orders):
            init = p.get("initial") or {}
            text = init.get("text") or p.get("text") or ""
            out.append({
                "id": 100 + i + 1,
                "initial": {
                    "text": text,
                    "reduce_only": init.get("reduce_only") or p.get("reduce_only") or 0,
                    "size": init.get("size") or p.get("size") or 0,
                },
                "text": text,
                "status": p.get("status") or "untriggered",
            })
        return out

    def cancel_price_order(self, oid):
        self.cancelled.append(str(oid))
        return {"cancelled": oid}

    def get_positions(self):
        return list(self.positions)


def po(text, sz, ro=1, st="untriggered"):
    return {"initial": {"text": text, "reduce_only": ro, "size": sz}, "status": st}


def long_pos(n=30):
    return {"contract": "BTC_USDT", "size": n, "mode": "single"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), name, detail)
    return cond


def run_case(name, price_orders, positions, expect_cancel_ids, expect_keep_ids):
    """expect_*_ids: FakeClient 回传的 101+ 序号。"""
    client = FakeClient(price_orders, positions)
    ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
    cancelled = ex._cleanup_orphan_protectors("BTC_USDT")
    cancelled_ids = set(client.cancelled)
    ok1 = cancelled_ids == set(expect_cancel_ids) or set(cancelled) == set(expect_cancel_ids)
    ok2 = not (set(expect_keep_ids) & cancelled_ids)
    check(name, ok1 and ok2,
          f"cancelled={sorted(cancelled_ids)} expect={sorted(expect_cancel_ids)} kept={sorted(expect_keep_ids)}")


def main():
    SL, TP = "t-brk-sl", "t-brk-tp"
    ENTRY = "t-brk"
    OTHER = "t-x-tp"

    # id 映射：index i → 100+i+1
    # A: [TP(-10), SL(-30), t-brk-tp(+5)] → cancel id 103 only
    run_case(
        "A singleTP+SL + pos + 1 orphan",
        [po(TP, -10), po(SL, -30), po("t-brk-tp", 5)],
        [long_pos(30)],
        expect_cancel_ids={"103"},
        expect_keep_ids={"101", "102"},
    )

    # B: [TP, SL, t-brk-tp(+5), t-brk-sl(+5), OTHER] → cancel 103,104
    run_case(
        "B singleTP+SL + pos + 3 orphans",
        [po(TP, -10), po(SL, -30), po("t-brk-tp", 5), po("t-brk-sl", 5), po(OTHER, -5)],
        [long_pos(30)],
        expect_cancel_ids={"103", "104"},
        expect_keep_ids={"101", "102", "105"},
    )

    # C: 3TP+SL + short orphan
    run_case(
        "C multiTP3+SL + pos + 1 orphan",
        [po(TP, -10), po(TP, -10), po(TP, -10), po(SL, -30), po("t-brk-sl", 5)],
        [long_pos(30)],
        expect_cancel_ids={"105"},
        expect_keep_ids={"101", "102", "103", "104"},
    )

    # D: multiTP + short orphan + OTHER + ENTRY + t-x-sl
    run_case(
        "D multiTP + pos + 4 orphans",
        [
            po(TP, -10), po(TP, -10), po(SL, -30),
            po("t-brk-tp", 5),
            po(OTHER, -5),
            po(ENTRY, 0, ro=0),
            po("t-x-sl", 5),
        ],
        [long_pos(30)],
        expect_cancel_ids={"104"},
        expect_keep_ids={"101", "102", "103", "105", "106", "107"},
    )

    # E: flat → 本 bot TP/SL 变孤儿
    run_case(
        "E singleTP + flat + 1 orphan",
        [po(TP, -10), po(SL, -30), po(OTHER, -5)],
        [],
        expect_cancel_ids={"101", "102"},
        expect_keep_ids={"103"},
    )

    # F: flat multiTP
    run_case(
        "F multiTP3 + flat + 2 orphans",
        [po(TP, -10), po(TP, -10), po(TP, -10), po(OTHER, -5), po("t-x-sl", 5)],
        [],
        expect_cancel_ids={"101", "102", "103"},
        expect_keep_ids={"104", "105"},
    )

    # G: long multiTP kept + short orphans cancelled
    run_case(
        "G multiTP long + short orphans",
        [
            po(TP, -10), po(TP, -10), po(TP, -10),
            po("t-brk-tp", 5), po("t-brk-sl", 5),
            po(OTHER, -5),
        ],
        [long_pos(30)],
        expect_cancel_ids={"104", "105"},
        expect_keep_ids={"101", "102", "103", "106"},
    )

    # H: entry+protectors+orphan
    run_case(
        "H entry+protectors+orphan + pos",
        [po(ENTRY, 240, ro=0), po(TP, -120), po(SL, -120), po("t-brk-tp", 5)],
        [long_pos(240)],
        expect_cancel_ids={"104"},
        expect_keep_ids={"101", "102", "103"},
    )

    # I: only current plan
    run_case(
        "I only current plan multiTP",
        [po(TP, -10), po(TP, -10), po(SL, -20)],
        [long_pos(20)],
        expect_cancel_ids=set(),
        expect_keep_ids={"101", "102", "103"},
    )

    # J: all orphans flat
    run_case(
        "J all orphans flat",
        [po(TP, -10), po(TP, -10), po(OTHER, -1), po("t-x-sl", 1)],
        [],
        expect_cancel_ids={"101", "102"},
        expect_keep_ids={"103", "104"},
    )

    print("\n====", f"PASS {len(PASS)} FAIL {len(FAIL)}", "====")
    if FAIL:
        print("failed:", FAIL)
        return 2
    print("ALL MATRIX CASES OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
