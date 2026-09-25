"""Hybrid market snapshot for strategist prompts."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..gate_client import GateApiError, GateClient
from .indicators import attach_indicators, latest_indicators
from .market import MarketConfig, resolve_candles

TICKER_FIELDS = (
    "last",
    "mark_price",
    "index_price",
    "funding_rate",
    "funding_rate_indicative",
    "high_24h",
    "low_24h",
    "change_percentage",
    "change_price",
    "volume_24h_quote",
    "highest_bid",
    "lowest_ask",
    "total_size",
)

STATS_FIELDS = (
    "time",
    "open_interest",
    "open_interest_usd",
    "lsr_taker",
    "lsr_account",
    "top_lsr_account",
    "long_liq_size",
    "short_liq_size",
    "mark_price",
)


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(raw: dict, fields: tuple[str, ...]) -> dict[str, Any]:
    return {k: _f(raw.get(k)) if k != "time" else raw.get(k) for k in fields}


def collect_snapshot(
    client: GateClient,
    symbols: list[str],
    candles: int = 60,
    interval: str = "15m",
    market_cfg: Optional[MarketConfig] = None,
    env: str = "live",
    bot_root: Optional[Path] = None,
) -> dict[str, Any]:
    cfg = market_cfg or MarketConfig()
    refresh = set(cfg.refresh or [])
    market: dict[str, Any] = {}
    meta: dict[str, Any] = {
        "market_mode": cfg.mode,
        "candle_source": {},
        "degraded": [],
        "stale": [],
        "refresh": sorted(refresh),
    }

    for sym in symbols:
        entry: dict[str, Any] = {"symbol": sym}
        ticker_raw: dict = {}

        # ticker: last + funding + mark/index + 24h (one REST call)
        if "ticker" in refresh or "last" in refresh or not refresh:
            try:
                ticker_raw = client.get_ticker(sym)
                entry["last"] = _f(ticker_raw.get("last"))
                if "ticker" in refresh or not refresh:
                    entry["ticker"] = _pick(ticker_raw, TICKER_FIELDS)
            except GateApiError as e:
                entry["last_error"] = str(e)
                meta["degraded"].append(f"{sym}:last")
        else:
            try:
                entry["last"] = client.get_last_price(sym)
            except GateApiError as e:
                entry["last_error"] = str(e)
                meta["degraded"].append(f"{sym}:last")

        # contract meta: AI needs quanto / min unit to size positions correctly
        try:
            cm = client.get_contract(sym)
            last_px = entry.get("last") or _f((ticker_raw or {}).get("last"))
            min_notional = None
            if last_px and cm.quanto_multiplier:
                min_notional = float(last_px) * float(cm.quanto_multiplier)
            entry["contract"] = {
                "quanto_multiplier": cm.quanto_multiplier,
                "order_size_round": cm.order_size_round,
                "order_price_round": cm.order_price_round,
                "leverage_max": cm.leverage_max,
                "min_notional_usd": min_notional,
                "note": "张数=size_usd/(last*quanto); 1张≈min_notional_usd 名义",
            }
        except Exception as e:  # noqa: BLE001
            entry["contract_error"] = str(e)
            meta["degraded"].append(f"{sym}:contract")

        if "stats" in refresh:
            try:
                rows = client.get_contract_stats(sym, limit=1)
                if rows:
                    entry["stats"] = _pick(rows[-1], STATS_FIELDS)
            except Exception as e:  # noqa: BLE001
                entry["stats_error"] = str(e)
                meta["degraded"].append(f"{sym}:stats")

        if "orderbook" in refresh:
            try:
                ob = client.get_orderbook_top(sym, limit=5)
                entry["orderbook"] = {
                    "bids": [{"p": _f(b.get("p")), "s": _f(b.get("s"))} for b in ob.get("bids") or []],
                    "asks": [{"p": _f(a.get("p")), "s": _f(a.get("s"))} for a in ob.get("asks") or []],
                }
            except Exception as e:  # noqa: BLE001
                entry["orderbook_error"] = str(e)
                meta["degraded"].append(f"{sym}:orderbook")

        try:
            result = resolve_candles(
                client,
                sym,
                interval,
                candles,
                market_cfg=cfg,
                env=env,
                bot_root=bot_root,
            )
            rows = attach_indicators(list(result.rows), cfg.indicators or None)
            entry["candles"] = rows
            entry["candle_source"] = result.source
            entry["stale"] = result.stale
            meta["candle_source"][sym] = result.source
            if result.stale:
                meta["stale"].append(sym)
            for d in result.degraded:
                tag = f"{sym}:{d}"
                if tag not in meta["degraded"]:
                    meta["degraded"].append(tag)
            if result.error:
                entry["candles_error"] = result.error
            if rows:
                entry["indicators"] = latest_indicators(rows, cfg.indicators or None)

            # multi-timeframe extras (compact: last N bars + latest indicators)
            extra_tfs = [str(tf).lower() for tf in (getattr(cfg, "extra_timeframes", None) or [])]
            extra_tfs = [tf for tf in extra_tfs if tf and tf != str(interval).lower()]
            if extra_tfs:
                entry["tf"] = {}
                n_extra = int(getattr(cfg, "extra_candles", 20) or 20)
                for tf in extra_tfs:
                    try:
                        xres = resolve_candles(
                            client, sym, tf, n_extra,
                            market_cfg=cfg, env=env, bot_root=bot_root,
                        )
                        xrows = attach_indicators(list(xres.rows), cfg.indicators or None)
                        entry["tf"][tf] = {
                            "candles": xrows[-n_extra:] if xrows else [],
                            "indicators": latest_indicators(xrows, cfg.indicators or None) if xrows else {},
                            "source": xres.source,
                            "stale": xres.stale,
                        }
                        if xres.stale:
                            meta["stale"].append(f"{sym}:{tf}")
                        for d in xres.degraded:
                            tag = f"{sym}:{tf}:{d}"
                            if tag not in meta["degraded"]:
                                meta["degraded"].append(tag)
                    except Exception as e:  # noqa: BLE001
                        entry.setdefault("tf", {})[tf] = {"error": str(e)[:80]}
                        meta["degraded"].append(f"{sym}:{tf}:error")
        except Exception as e:  # noqa: BLE001
            entry["candles_error"] = str(e)
            meta["degraded"].append(f"{sym}:candles_error")
        market[sym] = entry

    account: dict[str, Any] = {}
    try:
        acc = client.get_account() or {}
        account = {
            "position_mode": acc.get("position_mode"),
            "available": acc.get("available"),
            "total": acc.get("total"),
        }
    except Exception as e:  # noqa: BLE001
        account = {"error": f"account: {e}"}
        meta["degraded"].append("account")
    if "error" not in account and account.get("available") in (None, ""):
        account["error"] = "account: missing available"
        meta["degraded"].append("account")
    if "error" not in account:
        try:
            account["positions"] = [
                {
                    "contract": p.get("contract"),
                    "mode": p.get("mode"),
                    "size": p.get("size"),
                    "entry_price": p.get("entry_price"),
                    "leverage": p.get("leverage"),
                }
                for p in (client.get_positions() or [])
                if int(p.get("size") or 0) != 0
            ]
        except Exception as e:  # noqa: BLE001
            account["error"] = f"positions: {e}"
            account.setdefault("positions", [])
            meta["degraded"].append("positions")

    return {
        "interval": interval,
        "candles_len": candles,
        "market": market,
        "account": account,
        "meta": meta,
    }
