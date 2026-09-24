"""Market + account snapshot for strategist prompts."""
from __future__ import annotations

from typing import Any

from ..gate_client import GateApiError, GateClient


def collect_snapshot(
    client: GateClient,
    symbols: list[str],
    candles: int = 60,
    interval: str = "15m",
) -> dict[str, Any]:
    market: dict[str, Any] = {}
    for sym in symbols:
        entry: dict[str, Any] = {"symbol": sym}
        try:
            entry["last"] = client.get_last_price(sym)
        except GateApiError as e:
            entry["error"] = str(e)
        try:
            raw = client.public_get(
                "/api/v4/futures/usdt/candlesticks",
                f"contract={sym}&interval={interval}&limit={candles}",
            )
            rows = []
            for r in raw or []:
                if isinstance(r, (list, tuple)) and len(r) >= 6:
                    rows.append({"t": r[0], "v": r[1], "c": r[2], "h": r[3], "l": r[4], "o": r[5]})
                elif isinstance(r, dict):
                    rows.append(r)
            entry["candles"] = rows
        except Exception as e:  # noqa: BLE001
            entry["candles_error"] = str(e)
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
        account["error"] = str(e)

    return {"interval": interval, "candles_len": candles, "market": market, "account": account}
