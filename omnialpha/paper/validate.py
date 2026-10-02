"""合约精度与下单校验（对标交易所拒绝行为）。

价格对齐 tick、数量对齐 lot、最小名义、价格带、杠杆上限。
Gate 语义：price="0"/None 表示市价；tif: GTC/IOC/FOK/PO/POC。
"""
from __future__ import annotations

from typing import Any, Optional


class PaperReject(Exception):
    """下单/改单被拒，message 用交易所风格原因。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _round_to(value: float, step: float) -> float:
    """向最近的 step 整数倍取整（与 exchange round_price 一致）。"""
    if step is None or step <= 0:
        return value
    return round(round(value / step) * step, 10)


def _norm_tif(tif: str) -> str:
    t = str(tif or "GTC").upper()
    if t in ("POC", "PO", "POST_ONLY", "MAKER_ONLY"):
        return "PO"
    if t in ("GTC", "IOC", "FOK", "PO"):
        return t
    raise PaperReject(f"invalid tif: {tif}")


def validate_and_round_order(
    body: dict,
    contract_meta: Any,
    last_price: float,
    *,
    price_band_pct: float = 5.0,
    leverage: float = 20.0,
    leverage_max: Optional[float] = None,
    available: float = 0.0,
    min_notional_usd: Optional[float] = None,
) -> dict:
    """校验并把 body 规范化，失败抛 PaperReject。

    Gate 语义：price=0/None + tif=ioc → 市价；否则限价。
    """
    size = body.get("size")
    if size is None:
        raise PaperReject("size required")
    try:
        size = float(size)
    except (TypeError, ValueError):
        raise PaperReject("invalid size")
    if size == 0:
        raise PaperReject("size must be non-zero")

    lot = float(getattr(contract_meta, "order_size_round", 0) or 0)
    if lot > 0:
        rounded = _round_to(abs(size), lot)
        if rounded <= 0:
            raise PaperReject("size below lot size")
        size = rounded if size > 0 else -rounded

    quanto = float(getattr(contract_meta, "quanto_multiplier", 1) or 1)
    price_round = float(getattr(contract_meta, "order_price_round", 0) or 0)
    lev_cap = float(leverage_max or getattr(contract_meta, "leverage_max", 100) or 100)

    if leverage and lev_cap and float(leverage) > lev_cap:
        raise PaperReject(f"leverage too high: {leverage} > {lev_cap}")

    # Gate 语义：price=0/None 表示市价
    raw_price = body.get("price")
    price: Optional[float] = None
    is_market = False
    if raw_price in (None, "", 0, "0", 0.0):
        is_market = True
        otype = "market"
    else:
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            raise PaperReject("invalid price")
        if price <= 0:
            is_market = True
            otype = "market"
        else:
            otype = "limit"

    if not is_market:
        if price_round > 0:
            price = _round_to(price, price_round)
            if price <= 0:
                raise PaperReject("price below tick size")
        if last_price and price_band_pct > 0:
            lo = last_price * (1 - price_band_pct / 100.0)
            hi = last_price * (1 + price_band_pct / 100.0)
            if not (lo <= price <= hi):
                raise PaperReject(f"price out of band: {price} not in [{lo:.6g},{hi:.6g}]")

    ref = price if price is not None else (last_price or 0)
    notional = abs(size) * quanto * ref
    mnc = float(min_notional_usd or getattr(contract_meta, "min_notional_usd", 0) or 0)
    if mnc > 0 and notional < mnc:
        raise PaperReject(f"min notional: {notional:.6g} < {mnc:.6g}")

    reduce_only = bool(body.get("reduce_only"))
    if not reduce_only and available is not None and available > 0:
        need = notional / max(leverage or 1.0, 1.0)
        if need > available + 1e-9:
            raise PaperReject(f"insufficient available: need {need:.6g}, have {available:.6g}")

    tif = _norm_tif(body.get("tif"))
    # 市价单默认 IOC（Gate 语义）
    if is_market and tif == "GTC":
        tif = "IOC"

    out = dict(body)
    out["size"] = size
    out["price"] = price
    out["type"] = otype
    out["tif"] = tif
    out["reduce_only"] = 1 if reduce_only else 0
    return out
