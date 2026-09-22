"""Convert strategy size_usd into Gate futures contract counts."""

from __future__ import annotations

import math
from typing import Optional

from .gate_client import ContractMeta, GateApiError


def pct_to_size_usd(pct: float, base_usdt: float) -> float:
    """Convert asset ratio to notional size_usd. pct is 0..1 (0.1 = 10%)."""
    if pct is None or pct <= 0 or pct > 1:
        raise GateApiError(f"pct must be in (0, 1], got {pct!r}")
    if base_usdt is None or base_usdt <= 0:
        raise GateApiError(f"base_usdt must be positive, got {base_usdt!r}")
    return float(base_usdt) * float(pct)


def usd_to_contracts(
    size_usd: float,
    entry_price: float,
    meta: ContractMeta,
) -> int:
    """contracts = size_usd / (price * quanto_multiplier), rounded down to lot size."""
    if size_usd is None or size_usd <= 0:
        raise GateApiError("size_usd must be positive")
    if entry_price is None or entry_price <= 0:
        raise GateApiError("entry_price must be positive")
    multiplier = meta.quanto_multiplier or 1.0
    if multiplier <= 0:
        multiplier = 1.0
    raw = size_usd / (entry_price * multiplier)
    lot = meta.order_size_round or 1.0
    if lot <= 0:
        lot = 1.0
    # Gate futures size is an integer contract count; floor to lot when lot >= 1
    contracts = int(math.floor(raw + 1e-12))
    if lot > 1:
        contracts = (contracts // int(lot)) * int(lot)
    if contracts < 1:
        raise GateApiError(
            f"size_usd={size_usd} too small at price={entry_price} "
            f"(need >= {entry_price * multiplier:.6f} USDT per 1 contract)"
        )
    return contracts


def default_trigger_limit_price(
    trigger_price: float,
    side: str,
    is_tp: bool,
    slip_ratio: float = 0.001,
) -> float:
    """Mirror quick_order guidance: sell a bit below trigger, buy a bit above."""
    if trigger_price <= 0:
        raise GateApiError("trigger_price must be positive")
    slip = trigger_price * slip_ratio
    if is_tp:
        # take profit close: sell (long) near trigger → slightly below
        return trigger_price - slip if side == "long" else trigger_price + slip
    # stop loss close: aggressive exit
    return trigger_price - slip if side == "long" else trigger_price + slip


def round_price(price: float, meta: ContractMeta) -> float:
    step = meta.order_price_round or 0
    if step <= 0:
        return price
    precision = max(0, int(round(-math.log10(step))) if step < 1 else 0)
    rounded = round(price / step) * step
    return float(f"{rounded:.{precision}f}")
