"""保证金与强平引擎 + 资金费率结算（复刻交易所风控）。

- 初始保证金 = 名义 / 杠杆
- 维持保证金率（默认 0.5%）反推强平价
- mark/last 触及强平价 → 强制平仓（写 fills，role=liquidation）
- 每 8h 结算真实资金费率（多付/空收随费率符号）
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .engine import PaperEngine
from .store import PaperStore

FUNDING_INTERVAL_SEC = 8 * 3600


def liquidation_price(
    entry: float, size: float, leverage: float,
    maintenance_margin_rate: float = 0.005,
    margin: Optional[float] = None,
) -> Optional[float]:
    """逐仓强平价：反推当维持保证金耗尽时的价格。

    多头：liq = entry * (1 - 1/lev + mmr)
    空头：liq = entry * (1 + 1/lev - mmr)
    """
    if not size or not entry:
        return None
    lev = max(float(leverage or 1), 1.0)
    mmr = float(maintenance_margin_rate or 0.005)
    if size > 0:
        return entry * (1.0 - 1.0 / lev + mmr)
    return entry * (1.0 + 1.0 / lev - mmr)


class RiskEngine:
    def __init__(self, store: PaperStore, feed: Any, engine: PaperEngine):
        self.store = store
        self.feed = feed
        self.engine = engine

    # ── 强平 ───────────────────────────────────────────
    def check_liquidations(self) -> list[dict]:
        """扫描持仓，触及强平价则强制平仓。返回被平仓位列表。"""
        mmr = self.store.cfg_f("maintenance_margin_rate", 0.005)
        closed: list[dict] = []
        for p in self.store.get_positions():
            size = float(p.get("size") or 0)
            if not size:
                continue
            symbol = p["contract"]
            entry = float(p.get("entry_price") or 0)
            lev = float(p.get("leverage") or 20)
            liq = p.get("liquidation_price") or liquidation_price(entry, size, lev, mmr)
            if liq is None:
                continue
            # 先补写强平价
            if not p.get("liquidation_price"):
                self.store.upsert_position(
                    symbol, p.get("mode") or "single",
                    liquidation_price=liq,
                    size=size, entry_price=entry, leverage=lev,
                    margin=float(p.get("margin") or 0),
                    margin_mode=p.get("margin_mode") or "isolated",
                    realised_pnl=float(p.get("realised_pnl") or 0),
                )
            last = float(self.feed.get_last_price(symbol) or 0)
            hit = (last <= liq) if size > 0 else (last >= liq)
            if not hit:
                continue
            # 强制平仓：反向市价单，role=liquidation（只写一条 fill）
            side_size = -size
            self.engine._apply_fill(
                {
                    "order_id": f"liq-{symbol}-{int(time.time())}",
                    "contract": symbol,
                    "size": side_size,
                    "reduce_only": 1,
                },
                price=last,
                size=side_size,
                role="liquidation",
            )
            closed.append({"contract": symbol, "price": last, "liquidation_price": liq, "size": size})
        return closed

    # ── 资金费率 ───────────────────────────────────────
    def settle_funding(self, force: bool = False) -> list[dict]:
        """每 8h 对持仓结算资金费率。费率取绑定所真实值。"""
        if not self.store.cfg_s("funding_enabled", "1") in ("1", "true", "True", "yes"):
            return []
        now = int(time.time())
        last = self.store.last_funding_time()
        # 对齐 8h 边界，避免启动即结算
        boundary = (now // FUNDING_INTERVAL_SEC) * FUNDING_INTERVAL_SEC
        if not force:
            if last and (now - last) < FUNDING_INTERVAL_SEC:
                return []
            if not last and now < boundary + 60:
                return []
        out: list[dict] = []
        for p in self.store.get_positions():
            size = float(p.get("size") or 0)
            if not size:
                continue
            symbol = p["contract"]
            try:
                tk = self.feed.get_ticker(symbol) or {}
                rate = float(tk.get("funding_rate") or 0)
            except Exception:  # noqa: BLE001
                rate = 0.0
            last_px = float(self.feed.get_last_price(symbol) or 0)
            try:
                quanto = float(getattr(self.feed.get_contract(symbol), "quanto_multiplier", 1) or 1)
            except Exception:  # noqa: BLE001
                quanto = 1.0
            # 多头付、空头收（rate>0 时）
            amount = -rate * abs(size) * quanto * last_px * (1 if size > 0 else -1)
            if abs(amount) > 1e-12:
                acct = self.store.get_account()
                bal = float(acct.get("balance") or 0) + amount
                self.store.update_account(balance=bal)
            self.store.insert_funding({
                "settle_time": now,
                "contract": symbol,
                "rate": rate,
                "amount": amount,
                "position_side": "long" if size > 0 else "short",
            })
            out.append({"contract": symbol, "rate": rate, "amount": amount})
        return out
