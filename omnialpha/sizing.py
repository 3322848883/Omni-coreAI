"""Convert strategy size_usd into Gate futures contract counts."""

from __future__ import annotations

import logging
import math
from typing import Optional

from .gate_client import ContractMeta, GateApiError

log = logging.getLogger(__name__)


def vol_adjust_size(base_size_usd: float, atr_pct: float,
                    target_pct: float = 2.0,
                    clamp_lo: float = 0.5, clamp_hi: float = 2.0) -> float:
    """ATR 目标波动缩放仓位。波动大时减仓，波动小时加仓。

    base_size_usd: 基准仓位
    atr_pct: 当前 ATR 百分比（如 2.0 = 2%）
    target_pct: 目标波动百分比（0=禁用）
    clamp_lo/hi: 缩放倍数上下限，防极端值
    """
    if not base_size_usd or base_size_usd <= 0:
        return base_size_usd or 0
    if not target_pct or target_pct <= 0:
        return base_size_usd
    if not atr_pct or atr_pct <= 0:
        return base_size_usd
    ratio = target_pct / atr_pct
    ratio = max(clamp_lo, min(clamp_hi, ratio))
    return round(base_size_usd * ratio, 2)


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
        # **兜底：抬到最小可下量，不抛错**。
        # `size_usd` 是模型按「风险预算 ÷ 止损距离」反推的，小账户配远止损时
        # 会算出小于 1 张的名义（实测 16.15 < 25.76）—— 抛错会让**整轮计划作废**
        # 并回滚已挂的腿。抬到 1 张后风险略高于模型预期，但远小于「整轮不成交」
        # 的代价；真正的风险闸门是 yaml 风控 + 账户级上限，不靠这一条拦。
        floor_contracts = int(lot) if lot > 1 else 1
        log.warning(
            "size_usd=%.4f 不足 1 张（价 %.2f 需 >= %.4f），已抬到 %d 张",
            size_usd, entry_price, entry_price * multiplier, floor_contracts,
        )
        return floor_contracts
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
