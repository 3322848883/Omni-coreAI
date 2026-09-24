# -*- coding: utf-8 -*-
"""Testnet: execute every order type through parse_signal → Executor.

Keys come from env only (GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET).
Safe: testnet only, small sizes, cleanup + flatten at the end.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.gate_client import GateApiError, GateClient  # noqa: E402
from gate_bot.executor import Executor  # noqa: E402
from gate_bot.schema import parse_signal  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    RESULTS.append((name, status, str(detail)[:100]))
    print("%s  %-32s %s" % (status, name, str(detail)[:100]))
    return ok


def main() -> int:
    key = os.environ.get("GATE_TESTNET_API_KEY") or ""
    secret = os.environ.get("GATE_TESTNET_API_SECRET") or ""
    if not key or not secret:
        print("missing GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET")
        return 2

    c = GateClient(key, secret, env="testnet")
    ex = Executor(c, symbols_whitelist=["BTC_USDT", "ETH_USDT", "SOL_USDT"], max_notional_usd=200)

    last = c.get_last_price("BTC_USDT")
    eth_last = c.get_last_price("ETH_USDT")
    sol_last = c.get_last_price("SOL_USDT")
    mode = c.get_position_mode()
    dual = c.is_dual_position_mode()
    print("ENV=testnet MODE=%s DUAL=%s BTC=%s ETH=%s SOL=%s" % (mode, dual, last, eth_last, sol_last))
    print("=" * 72)

    def px(bps: float, base: float = None) -> float:
        """bps: +100 = +1% above base (default BTC last)."""
        b = last if base is None else base
        return round(b * (1 + bps / 10000.0), 1)

    def run(name, payload, expect_ok=True):
        try:
            rep = ex.execute_signal(parse_signal(payload))
            s = rep.results[0] if rep.results else None
            if s is None:
                return rec(name, False, "no step")
            ok = s.ok == expect_ok
            detail = s.error or json.dumps(
                {"as": (s.detail or {}).get("executed_as"), "id": ((s.detail or {}).get("order") or {}).get("id")},
                ensure_ascii=False,
            )
            return rec(name, ok, detail)
        except Exception as e:  # noqa: BLE001
            return rec(name, False, "EXC %s" % e)

    # ── 0 cleanup ──────────────────────────────────────────
    print("\n[0] cleanup flat")
    for sym in ("BTC_USDT", "ETH_USDT", "SOL_USDT"):
        for n in (20, 5):
            for sgn in (1, -1):
                try:
                    c.place_order({
                        "contract": sym, "size": sgn * n, "price": "0", "tif": "ioc",
                        "reduce_only": True, "text": "t-cln",
                    })
                except GateApiError:
                    pass
    try:
        flat = all(int(p.get("size") or 0) == 0 for p in c.get_positions())
        rec("cleanup_flat", flat, "positions")
    except GateApiError as e:
        rec("cleanup_flat", False, str(e))

    # ── 1 no-exec actions ──────────────────────────────────
    print("\n[1] no-exec actions")
    run("hold", {"action": "hold"})
    run("watch", {"action": "watch"})
    run("skip", {"action": "skip"})
    run("empty_action", {"symbol": "BTC_USDT"})
    run("empty_orders", {"orders": []})

    # ── 2 rejects ──────────────────────────────────────────
    print("\n[2] guards")
    run("whitelist_reject", {"action": "open_long", "symbol": "DOGE_USDT", "size": 1, "type": "market"}, expect_ok=False)
    run("notional_reject", {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 99999}, expect_ok=False)
    run("tiny_size_reject", {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 0.01}, expect_ok=False)

    # ── 3 order types: market / limit / post_only ──────────
    print("\n[3] order types")
    run("market_long", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "t-mktL"})
    run("market_short", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "t-mktS"})
    run("limit_long", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit",
                       "price": px(-200), "label": "t-limL"})
    run("limit_short", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "limit",
                        "price": px(+200), "label": "t-limS"})
    run("post_only_long", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "post_only",
                           "price": px(-150), "label": "t-poL"})
    run("post_only_short", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "post_only",
                            "price": px(+150), "label": "t-poS"})
    run("ioc_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "ioc",
                    "price": px(-800), "label": "t-ioc"})
    run("fok_far_reject", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "fok",
                           "price": px(-800), "label": "t-fok"}, expect_ok=False)

    # FOK that can fill: reduce into bid
    try:
        c.place_order({"contract": "BTC_USDT", "size": 2, "price": "0", "tif": "ioc", "text": "t-foksrc"})
        ob = c.public_get("/api/v4/futures/usdt/order_book", "contract=BTC_USDT&limit=1")
        bid = str(ob["bids"][0]["p"])
        r = c.place_order({"contract": "BTC_USDT", "size": -2, "price": bid, "tif": "fok",
                           "reduce_only": True, "text": "t-fokfill"})
        rec("fok_fill_reduce", int(r.get("left") or 0) == 0 or r.get("status") == "finished", r.get("id"))
    except GateApiError as e:
        rec("fok_fill_reduce", False, str(e))

    # ── 4 sizing variants ──────────────────────────────────
    print("\n[4] sizing")
    run("size_usd", {"action": "add_long", "symbol": "BTC_USDT", "size_usd": 50, "type": "market", "label": "t-usd"})
    run("size_pct", {"action": "add_long", "symbol": "BTC_USDT", "size_pct": 0.02, "type": "market", "label": "t-pct"})
    run("margin_pct", {"action": "add_long", "symbol": "BTC_USDT", "margin_pct": 0.02, "leverage": 5,
                       "type": "market", "label": "t-mpct"})
    run("size_contracts", {"action": "add_long", "symbol": "ETH_USDT", "size": 1, "type": "market", "label": "t-sz"})

    # ── 5 TP/SL trigger vs limit_order ─────────────────────
    print("\n[5] TP/SL modes")
    run("tpsl_trigger", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                         "tp": px(+500), "sl": px(-500), "tp_mode": "trigger", "sl_mode": "trigger",
                         "trigger_price_type": "mark", "label": "t-tpsl-t"})
    # limit_order TP/SL must stay inside Gate's price band (testnet rejects far ticks)
    run("tpsl_limit_order", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                             "tp": px(+80), "sl": px(-80), "tp_mode": "limit_order", "sl_mode": "limit_order",
                             "label": "t-tpsl-l"})
    run("tp_limit_sl_trigger", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                                "tp": px(+60), "sl": px(-60), "tp_mode": "limit_order", "sl_mode": "trigger",
                                "trigger_price_type": "mark", "label": "t-tpsl-mix"})
    run("tpsl_trigger_limit_type", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                                    "tp": px(+600), "sl": px(-600), "tp_type": "limit", "sl_type": "limit",
                                    "tp_limit_price": px(+590), "sl_limit_price": px(-590),
                                    "trigger_price_type": "mark", "label": "t-tpsl-tl"})

    # ── 6 stop_entry breakout ──────────────────────────────
    print("\n[6] stop_entry (breakout ≠ stop-loss)")
    run("stop_entry_long", {"action": "stop_entry_long", "symbol": "BTC_USDT", "size": 1,
                            "trigger_price": px(+300), "label": "t-seL"})
    run("buy_stop_alias", {"action": "buy_stop", "symbol": "BTC_USDT", "size": 1,
                           "trigger_price": px(+400), "label": "t-bs"})
    run("stop_entry_short", {"action": "stop_entry_short", "symbol": "BTC_USDT", "size": 1,
                             "trigger_price": px(-300), "label": "t-seS"})
    run("sell_stop_alias", {"action": "sell_stop", "symbol": "BTC_USDT", "size": 1,
                            "trigger_price": px(-400), "label": "t-ss"})

    # ── 7 add / reduce ─────────────────────────────────────
    print("\n[7] add / reduce")
    run("add_long", {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "t-al"})
    run("add_short", {"action": "add_short", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "t-as"})
    run("reduce_long", {"action": "reduce_long", "symbol": "BTC_USDT", "size": 1, "label": "t-rl"})
    # single mode nets add_short into the book — short may not exist
    pos_sizes = {p.get("contract"): int(p.get("size") or 0) for p in c.get_positions()}
    btc_sz = pos_sizes.get("BTC_USDT", 0)
    run("reduce_short", {"action": "reduce_short", "symbol": "BTC_USDT", "size": 1, "label": "t-rs"},
        expect_ok=(dual or btc_sz < 0))
    if dual:
        run("reduce_dual_no_side", {"action": "reduce", "symbol": "BTC_USDT", "size": 1}, expect_ok=False)
        run("reduce_dual_side_long", {"action": "reduce", "symbol": "BTC_USDT", "side": "long", "size": 1, "label": "t-rd"})
    else:
        run("reduce_single", {"action": "reduce", "symbol": "BTC_USDT", "size": 1, "label": "t-r"})

    # ── 8 grid / multi-leg ─────────────────────────────────
    print("\n[8] grid / orders[]")
    run("grid_long", {"action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
                      "levels": [{"price": px(-250), "size": 1}, {"price": px(-350), "size": 1}],
                      "label": "t-gL"})
    run("orders_multi", {"orders": [
        {"action": "add_long", "symbol": "ETH_USDT", "size": 1, "type": "limit",
         "price": px(-300, eth_last), "label": "t-m1"},
        {"action": "add_short", "symbol": "ETH_USDT", "size": 1, "type": "limit",
         "price": px(+300, eth_last), "label": "t-m2"},
    ]})

    # ── 9 replace / cancel / close ─────────────────────────
    print("\n[9] replace / cancel / close")
    run("replace_symbol_open", {"action": "open_long", "symbol": "SOL_USDT", "size": 1, "type": "limit",
                                "price": px(-200), "replace": "symbol", "label": "t-rp"})
    run("cancel_all", {"action": "cancel_all", "symbol": "BTC_USDT"})
    run("cancel_price_all", {"action": "cancel_price_all", "symbol": "BTC_USDT"})
    run("close_partial", {"action": "close", "symbol": "BTC_USDT", "size": 1, "label": "t-c1"})
    if dual:
        run("close_side", {"action": "close", "symbol": "BTC_USDT", "side": "long", "size": 1, "label": "t-cs"})
    run("close_all", {"action": "close_all", "symbol": "BTC_USDT", "label": "t-ca"})
    run("flatten_eth", {"action": "flatten", "symbol": "ETH_USDT", "label": "t-f"})
    run("flatten_sol", {"action": "flatten", "symbol": "SOL_USDT", "label": "t-f2"})

    # ── 10 leverage / margin_mode ──────────────────────────
    print("\n[10] leverage / margin")
    run("leverage_set", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                         "leverage": 5, "label": "t-lev"})
    run("margin_isolated", {"action": "open_long", "symbol": "ETH_USDT", "size": 1, "type": "market",
                            "margin_mode": "isolated", "label": "t-iso"})

    # ── 11 trail (known limited) ───────────────────────────
    print("\n[11] trail (known: fund password / testnet invalid)")
    s = run("trail", {"action": "trail", "symbol": "BTC_USDT", "amount": -1, "price_offset": "0.5",
                      "activation_price": "0"}, expect_ok=False)
    # trail may raise business code; either way document

    # ── 12 inbox watcher path (end-to-end) ─────────────────
    print("\n[12] watcher process_file end-to-end")
    try:
        from gate_bot.config import BotConfig
        from gate_bot.watcher import ProjectPaths, process_file

        tmp_root = ROOT / ".order_type_test"
        paths = ProjectPaths(tmp_root)
        paths.ensure()
        bot_id = "otype"
        inbox = paths.bot_inbox(bot_id)
        inbox.mkdir(parents=True, exist_ok=True)
        payload = {
            "action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit",
            "price": px(-400), "label": "t-e2e",
            "meta": {"signal_id": "e2e-otype"},
        }
        p = inbox / "e2e.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        bot = BotConfig(bot_id=bot_id, env="testnet", symbols=["BTC_USDT", "ETH_USDT", "SOL_USDT"],
                        max_notional_usd=200, api_key_env="GATE_TESTNET_API_KEY",
                        api_secret_env="GATE_TESTNET_API_SECRET")
        # process_file returns bool; use free policy so open is allowed with existing pos
        bot.position_policy = "free"
        ok = process_file(p, bot, paths, executor=None)
        rec("watcher_process_file", bool(ok), "done=%s" % ok)
    except Exception as e:  # noqa: BLE001
        rec("watcher_process_file", False, traceback.format_exc()[-120:])
        print(traceback.format_exc())

    # ── 13 final cleanup ───────────────────────────────────
    print("\n[13] final flatten")
    for sym in ("BTC_USDT", "ETH_USDT", "SOL_USDT"):
        # empty flatten is a no-op success after the POSITION_EMPTY fix
        run("final_flat_" + sym, {"action": "flatten", "symbol": sym, "label": "t-fin"}, expect_ok=True)
    try:
        c.cancel_price_orders("BTC_USDT") if hasattr(c, "cancel_price_orders") else None
    except Exception:  # noqa: BLE001
        pass
    try:
        for sym in ("BTC_USDT", "ETH_USDT", "SOL_USDT"):
            run("final_cancel_" + sym, {"action": "cancel_all", "symbol": sym})
            run("final_cancelp_" + sym, {"action": "cancel_price_all", "symbol": sym})
    except Exception:  # noqa: BLE001
        pass

    # summary
    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("SUMMARY  PASS=%d  FAIL=%d  TOTAL=%d" % (n_pass, n_fail, len(RESULTS)))
    if n_fail:
        print("---- failures ----")
        for name, s, d in RESULTS:
            if s == "FAIL":
                print("  FAIL %s | %s" % (name, d))
    # write machine-readable log
    out = ROOT / "logs" / "order_type_test.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"results": RESULTS, "mode": mode, "dual": dual, "last": last}, ensure_ascii=False) + "\n")
    print("log:", out)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
