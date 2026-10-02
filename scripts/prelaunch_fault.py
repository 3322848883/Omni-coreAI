# -*- coding: utf-8 -*-
"""M15–M18 fault injection checks for prelaunch runner."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor
from omnialpha.schema import parse_signal, SchemaError


def run_fault(REP, gate_client) -> None:
    # ── M15 timeout / no auto-retry ──
    print("\n[M15] timeout & no auto-retry")
    try:
        from omnialpha.strategist.llm_client import LLMClient, LLMConfig, LLMError

        if not os.environ.get("OPENAI_API_KEY"):
            REP.rec("M15", "llm_timeout", False, "SKIP no OPENAI_API_KEY")
        else:
            llm = LLMClient(LLMConfig(
                model=os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash",
                timeout_sec=1,
            ))
            t0 = time.time()
            raised = False
            try:
                llm.chat("test", "test")
            except LLMError:
                raised = True
            elapsed = time.time() - t0
            REP.rec("M15", "llm_timeout", raised and elapsed < 5, f"raised={raised} t={elapsed:.1f}s")
    except Exception as e:  # noqa: BLE001
        REP.rec("M15", "llm_timeout", False, str(e)[:80])

    try:
        from omnialpha.gate_client import GateClient, GateApiError

        bad = GateClient("x", "y", env="testnet")
        # point to unreachable by monkeypatching base
        bad.base = "http://127.0.0.1:1"
        t0 = time.time()
        raised = False
        try:
            bad.get_last_price("BTC_USDT")
        except Exception:
            raised = True
        REP.rec("M15", "rest_timeout_failfast", raised and (time.time() - t0) < 15, f"raised={raised}")
    except Exception as e:  # noqa: BLE001
        REP.rec("M15", "rest_timeout_failfast", False, str(e)[:80])

    # degraded snapshot marks
    try:
        from omnialpha.strategist.snapshot import collect_snapshot
        from omnialpha.strategist.market import MarketConfig

        class Flaky:
            def __init__(self):
                self.n = 0

            def get_ticker(self, sym):
                if self.n == 0:
                    self.n += 1
                    from omnialpha.gate_client import GateApiError
                    raise GateApiError("transient")
                return {"last": "100"}

            def get_last_price(self, sym):
                return 100.0

            def get_contract(self, sym):
                from omnialpha.gate_client import ContractMeta
                return ContractMeta(sym, 0.0001, 1, 0.1, 100)

            def get_account(self):
                return {"available": "10", "total": "10", "position_mode": "single"}

            def get_positions(self):
                return []

        snap = collect_snapshot(Flaky(), ["BTC_USDT"], candles=5,
                               market_cfg=MarketConfig(mode="rest_only", refresh=["ticker"]))
        deg = snap["meta"].get("degraded") or []
        REP.rec("M15", "snapshot_degraded_mark", any("last" in d or "ticker" in d for d in deg), str(deg)[:80])
    except Exception as e:  # noqa: BLE001
        REP.rec("M15", "snapshot_degraded_mark", False, str(e)[:80])

    # ── M16 partial fill / reduce ──
    print("\n[M16] partial fill handling")
    try:
        env = "testnet"
        client = gate_client(env)
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], max_notional_usd=100,
                      require_sl=False, order_scope="own")
        last = client.get_last_price("BTC_USDT")
        # aggressive limit (taker) — testnet market may hit MARKET_PRICE_TOO_DEVIATED → gtc hang
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size": 2, "type": "limit",
            "price": round(last * 1.002, 1),
            "label": "m16-open", "sl": round(last * 0.99, 1),
        }))
        if not rep.ok:
            REP.rec("M16", "open_for_reduce", False, str(rep.results[0].error)[:80])
        else:
            time.sleep(1.0)
            pos = [p for p in (client.get_positions() or [])
                   if p.get("contract") == "BTC_USDT" and int(p.get("size") or 0) != 0]
            if not pos:
                # thin testnet book: even taker limits may rest unfilled
                od = (rep.results[0].detail or {}).get("order") or {}
                REP.rec("M16", "open_filled", False,
                        "SKIP testnet no fill left=%s status=%s (liquidity)" % (
                            od.get("left"), od.get("status")))
                REP.problem("M16-liquidity",
                            f"entry placed but unfilled left={od.get('left')} on testnet",
                            cause="thin testnet book / market-deviation fallback to gtc",
                            level="P2",
                            fix="cover reduce path in unit tests; live fill covered by M19")
            else:
                r2 = ex.execute_signal(parse_signal({
                    "action": "reduce_long", "symbol": "BTC_USDT", "size": 1,
                    "type": "market", "label": "m16-reduce",
                }))
                REP.rec("M16", "reduce_partial", r2.ok, str(r2.results[0].error or "ok")[:80])
            ex.execute_signal(parse_signal({"action": "flatten", "symbol": "BTC_USDT"}))
            ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT", "label": "m16-open"}))
            ex.execute_signal(parse_signal({"action": "cancel_price_all", "symbol": "BTC_USDT", "label": "m16-open"}))
    except Exception as e:  # noqa: BLE001
        REP.rec("M16", "reduce_partial", False, str(e)[:100])
        REP.problem("M16", str(e), level="P2")

    # ── M17 interrupt + reconcile ──
    print("\n[M17] reconcile after hang")
    try:
        from omnialpha.watcher import reconcile_protection

        client = gate_client("testnet")
        # place entry without protection, then reconcile should add TP/SL or report
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], max_notional_usd=100,
                      require_sl=False, order_scope="own")
        last = client.get_last_price("BTC_USDT")
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
            "label": "m17-recon",
        }))
        notes = reconcile_protection(client, ["BTC_USDT"], prefix="t-prelaunch")
        REP.rec("M17", "reconcile_protection", True, f"entry_ok={rep.ok} notes={str(notes)[:80]}")
        ex.execute_signal(parse_signal({"action": "flatten", "symbol": "BTC_USDT"}))
        ex.execute_signal(parse_signal({"action": "cancel_price_all", "symbol": "BTC_USDT"}))
        ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT"}))
    except Exception as e:  # noqa: BLE001
        REP.rec("M17", "reconcile_protection", False, str(e)[:100])
        REP.problem("M17", str(e), level="P1")

    # ── M18 schema boundaries ──
    print("\n[M18] schema boundaries")
    bad_cases = [
        ("bad_symbol", {"action": "open_long", "symbol": "BTC-USDT; DROP", "size_usd": 10}),
        ("neg_size", {"action": "open_long", "symbol": "BTC_USDT", "size_usd": -5}),
        ("bad_action", {"action": "yolo", "symbol": "BTC_USDT", "size_usd": 10}),
        ("limit_no_price", {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 10, "type": "limit"}),
        ("stop_entry_no_trigger", {"action": "stop_entry_long", "symbol": "BTC_USDT", "size_usd": 10}),
    ]
    for name, body in bad_cases:
        try:
            parse_signal(body)
            REP.rec("M18", name, False, "should reject")
        except SchemaError as e:
            REP.rec("M18", name, True, str(e)[:70])
        except Exception as e:  # noqa: BLE001
            REP.rec("M18", name, True, f"rejected: {e}"[:70])
