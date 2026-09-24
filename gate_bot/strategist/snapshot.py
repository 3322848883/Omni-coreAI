"""Hybrid market snapshot for strategist prompts."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..gate_client import GateApiError, GateClient
from .indicators import attach_indicators, latest_indicators
from .market import MarketConfig, resolve_candles


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
    market: dict[str, Any] = {}
    meta: dict[str, Any] = {
        "market_mode": cfg.mode,
        "candle_source": {},
        "degraded": [],
        "stale": [],
    }

    for sym in symbols:
        entry: dict[str, Any] = {"symbol": sym}
        try:
            entry["last"] = client.get_last_price(sym)
        except GateApiError as e:
            entry["last_error"] = str(e)
            meta["degraded"].append(f"{sym}_last")

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
    except GateApiError as e:
        account = {"error": str(e)}
        meta["degraded"].append("account")
    except Exception as e:  # noqa: BLE001
        account = {"error": str(e)}
        meta["degraded"].append("account")

    return {
        "interval": interval,
        "candles_len": candles,
        "market": market,
        "account": account,
        "meta": meta,
    }
