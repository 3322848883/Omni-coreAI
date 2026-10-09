"""撮合与订单状态机（复刻交易所生命周期 + 盘口价成交）。

对齐 Gate/Executor 契约：
  - place_price_order 收嵌套 {initial:{contract,size,price,tif,text}, trigger:{rule,price_type,price}}
  - 订单 dict 带 id / text / status（对齐 Gate 字段）
  - price=0/None + tif=ioc 表示市价
  - 成交价：买→ask（卖一）、卖→bid（买一）
  - quanto_multiplier 贯穿 PnL/margin/funding
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .store import (
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_OPEN,
    ORDER_PARTIALLY_FILLED,
    ORDER_REJECTED,
    TRIGGER_CANCELLED,
    TRIGGER_FILLED,
    TRIGGER_TRIGGERED,
    TRIGGER_UNTRIGGERED,
    PaperStore,
    new_order_id,
)
from .validate import PaperReject, validate_and_round_order


def _now() -> int:
    return int(time.time())


def _order_view(order: dict) -> dict:
    """对齐 Gate 订单字段：id / text / status / size / price / tif。"""
    out = dict(order)
    out["id"] = order.get("order_id")
    return out


def _price_order_view(po: dict) -> dict:
    out = dict(po)
    out["id"] = po.get("order_id")
    return out


class PaperEngine:
    def __init__(self, store: PaperStore, feed: Any, alert_store: Optional[Any] = None):
        self.store = store
        self.feed = feed
        self.alert_store = alert_store
        # 合约乘数缓存：取值成功即缓存，供 feed 临时不可用时回退（**绝不兜底成 1.0**）
        self._quanto_cache: dict[str, float] = {}

    # ── 行情便捷 ─────────────────────────────────────────
    def _book(self, symbol: str) -> tuple[float, float]:
        ob = self.feed.get_orderbook_top(symbol, limit=5) or {}
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        bid = float(bids[0].get("p") or 0) if bids else 0.0
        ask = float(asks[0].get("p") or 0) if asks else 0.0
        if bid <= 0 or ask <= 0:
            last = float(self.feed.get_last_price(symbol) or 0)
            bid = bid or last
            ask = ask or last
        return bid, ask

    def _last(self, symbol: str) -> float:
        return float(self.feed.get_last_price(symbol) or 0)

    def _quanto(self, symbol: str, *, strict: bool = True) -> Optional[float]:
        """合约乘数（quanto_multiplier）—— **绝不静默兜底成 1.0**。

        它贯穿 notional / fee / margin / realised PnL：BTC_USDT 真实乘数是 0.0001，
        一旦退化成 1.0，上面四项**全部错 10,000 倍**。实测 `wyckoff-paper` 就因一次
        强平时取不到乘数，单笔手续费被记成 80,121（正确约 8 元），账户被打爆到 -70,573。

        策略：先取真值并缓存 → 取不到用缓存 → 从未取到过时：
        - `strict=True`（会计路径）抛 `PaperReject`，让这笔成交失败（宁可不成交，不算错账）
        - `strict=False`（展示路径）返回 None，由调用方按「未知」处理
        """
        try:
            q = float(getattr(self.feed.get_contract(symbol), "quanto_multiplier", 0) or 0)
            if q > 0:
                self._quanto_cache[symbol] = q
                return q
        except Exception:  # noqa: BLE001 — 交给下面的缓存/严格分支
            pass
        cached = self._quanto_cache.get(symbol)
        if cached:
            return cached
        if strict:
            raise PaperReject(
                f"quanto_multiplier unavailable for {symbol}; "
                "refuse to account with 1.0 (would be 10000x off)"
            )
        return None

    def _recompute_available(self) -> None:
        """available = balance - position_margin - order_margin（+ 未实现盈亏不占用）。"""
        acct = self.store.get_account()
        balance = float(acct.get("balance") or 0)
        pos_margin = float(acct.get("position_margin") or 0)
        order_margin = float(acct.get("order_margin") or 0)
        self.store.update_account(available=balance - pos_margin - order_margin)

    # ── 下单 ───────────────────────────────────────────
    def place_order(self, body: dict) -> dict:
        symbol = str(body.get("contract") or body.get("symbol") or "")
        if not symbol:
            raise PaperReject("contract required")
        meta = self.feed.get_contract(symbol)
        last = self._last(symbol)
        acct = self.store.get_account()
        # 杠杆优先级：订单显式 → **该合约的设置**（`leverage:<symbol>`）→ 账户级默认 → 20。
        # 逐合约那一层是必须的：真实交易所按合约设杠杆，账户级一份会让多币互相覆盖。
        lev = float(body.get("leverage")
                    or self.store.cfg_f(f"leverage:{symbol}", 0)
                    or acct.get("leverage") or 20)
        normalized = validate_and_round_order(
            body, meta, last,
            price_band_pct=self.store.cfg_f("price_band_pct", 5.0),
            leverage=lev,
            available=float(acct.get("available") or 0),
        )
        order_id = body.get("id") or body.get("client_order_id") or body.get("order_id") or new_order_id()

        tif = normalized["tif"]
        otype = normalized["type"]
        size = float(normalized["size"])
        price = normalized.get("price")

        order = {
            "order_id": order_id,
            "contract": symbol,
            "size": size,
            "price": price,
            "tif": tif,
            "type": otype,
            "status": ORDER_OPEN,
            "text": str(body.get("text") or ""),
            "filled_size": 0.0,
            "avg_price": None,
            "reduce_only": normalized["reduce_only"],
            "create_time": _now(),
            "finish_time": None,
            "error": None,
        }

        if tif == "PO" and self._is_taker(symbol, size, price, otype):
            order["status"] = ORDER_REJECTED
            order["error"] = "post only would take"
            order["finish_time"] = _now()
            self.store.insert_order(order)
            raise PaperReject("post only would take")

        self.store.insert_order(order)
        result = self.match_order(order_id)

        # IOC/FOK：未立即成交则撤销
        if tif in ("IOC", "FOK") and result.get("status") in (ORDER_OPEN, ORDER_PARTIALLY_FILLED):
            rolled_back = False
            if tif == "FOK" and float(result.get("filled_size") or 0) < abs(size):
                # FOK 全成或撤销：回滚成交
                self._rollback_fills(order_id, symbol)
                rolled_back = True
            self.store.update_order(order_id, status=ORDER_CANCELLED, finish_time=_now())
            fresh = dict(self.store.get_order(order_id) or result)
            # 部分成交仍可能已实现盈亏；回滚过就不算（仓位已冲销）
            if not rolled_back and result.get("pnl") is not None:
                fresh["pnl"] = result["pnl"]
                fresh["realised_pnl"] = result["pnl"]
            result = fresh
        return _order_view(result)

    def place_price_order(self, body: dict) -> dict:
        """触发单。支持 Gate 嵌套 {initial,trigger} 与扁平 body。"""
        initial = body.get("initial") or body
        trigger = body.get("trigger") or body

        symbol = str(initial.get("contract") or initial.get("symbol") or body.get("contract") or body.get("symbol") or "")
        if not symbol:
            raise PaperReject("contract required")

        size_raw = initial.get("size") or body.get("size")
        try:
            size = float(size_raw)
        except (TypeError, ValueError):
            raise PaperReject("invalid size")
        if size == 0:
            raise PaperReject("size required")

        trigger_price = trigger.get("price") or body.get("trigger_price")
        try:
            trigger_price = float(trigger_price)
        except (TypeError, ValueError):
            raise PaperReject("invalid trigger_price")
        if trigger_price <= 0:
            raise PaperReject("trigger_price must be positive")

        # rule: 1=price above（触发买/TP 空），2=price below（触发卖/SL 多）
        rule = int(trigger.get("rule") or 0)
        # Gate: size>0 为买单；触发后转 initial 方向
        side = "buy" if size > 0 else "sell"
        price_type = str(trigger.get("price_type") or "latest")
        # price_type 0/1/2 -> latest/mark/index（PRICE_TYPE_MAP）
        if price_type in ("0", "1", "2"):
            price_type = {"0": "latest", "1": "mark", "2": "index"}.get(price_type, "latest")

        from .store import new_order_id as _nid
        po = {
            "order_id": body.get("id") or body.get("order_id") or _nid(),
            "contract": symbol,
            "trigger_price": trigger_price,
            "trigger_price_type": price_type,
            "order_type": str(initial.get("type") or ("market" if str(initial.get("price") or "0") in ("", "0") else "limit")).lower(),
            "price": initial.get("price") if str(initial.get("price") or "0") not in ("", "0") else None,
            "size": size,
            "side": side,
            "rule": rule,
            "reduce_only": 1 if body.get("reduce_only") or initial.get("reduce_only") else 0,
            "status": TRIGGER_UNTRIGGERED,
            "text": str(initial.get("text") or body.get("text") or ""),
            "create_time": _now(),
            "trigger_time": None,
            "finish_time": None,
            "error": None,
        }
        self.store.insert_price_order(po)
        return _price_order_view(po)

    # ── 撮合 ───────────────────────────────────────────
    def _is_taker(self, symbol: str, size: float, price: Optional[float], otype: str) -> bool:
        if otype == "market":
            return True
        if price is None:
            return True
        bid, ask = self._book(symbol)
        return (ask <= price) if size > 0 else (bid >= price)

    def _fill_price(self, symbol: str, size: float) -> float:
        bid, ask = self._book(symbol)
        return ask if size > 0 else bid

    def match_order(self, order_id: str) -> dict:
        order = self.store.get_order(order_id)
        if not order:
            raise PaperReject("order not found", status=404)
        if order["status"] not in (ORDER_OPEN, ORDER_PARTIALLY_FILLED):
            return order
        # 已全部成交则禁止再次入账（防并发双花）
        if float(order.get("filled_size") or 0) >= abs(float(order["size"])) - 1e-12:
            self.store.update_order(order_id, status=ORDER_FILLED, finish_time=_now())
            return self.store.get_order(order_id) or order

        symbol = order["contract"]
        size = float(order["size"])
        price = order.get("price")
        otype = order.get("type") or "limit"
        bid, ask = self._book(symbol)

        hit = False
        if otype == "market":
            hit = True
        elif price is not None:
            if size > 0:
                hit = ask <= float(price)
            else:
                hit = bid >= float(price)
        if not hit:
            return order

        fill_px = self._fill_price(symbol, size)
        realised = self._apply_fill(order, fill_px, size, role="taker")
        out = self.store.get_order(order_id) or order
        # 平/减仓的已实现盈亏回传给调用方：executor 用它落 `detail.realized_pnl`，
        # 再供成交卡片与策略画像消费。原先只写进 fills 表，调用方永远拿不到。
        if realised:
            out = dict(out, pnl=realised, realised_pnl=realised)
        return out

    def _rollback_fills(self, order_id: str, symbol: str) -> None:
        """FOK 失败回滚成交与仓位（简化：按 fills 反向冲销）。"""
        fills = [f for f in self.store.list_fills(symbol, limit=200) if f.get("order_id") == order_id]
        for f in fills:
            self._update_position(symbol, -float(f["size"]) * (1 if f["side"] == "buy" else -1),
                                  float(f["price"]), reduce_only=False)
            acct = self.store.get_account()
            self.store.update_account(balance=float(acct.get("balance") or 0) + float(f.get("fee") or 0))

    def _apply_fill(self, order: dict, price: float, size: float, role: str = "taker") -> float:
        """入账一笔成交，返回本次的**已实现盈亏**（开仓为 0）。

        返回值供调用方回传（见 `match_order`）—— 原先只写进 fills 表，
        调用方（executor）拿不到，导致平仓步骤永远没有 `realized_pnl`。
        """
        symbol = order["contract"]
        filled = float(order.get("filled_size") or 0)
        total = float(order["size"])
        # 幂等：已成满则不再入账；本次成交量不超过剩余
        remaining = abs(total) - filled
        if remaining <= 1e-12:
            self.store.update_order(order["order_id"], status=ORDER_FILLED, finish_time=_now())
            return 0.0
        size = float(size)
        if abs(size) > remaining + 1e-12:
            size = remaining if size > 0 else -remaining
        avg = order.get("avg_price")
        new_filled = filled + abs(size)
        new_avg = price if not avg else (float(avg) * filled + price * abs(size)) / new_filled

        status = ORDER_FILLED if new_filled >= abs(total) - 1e-12 else ORDER_PARTIALLY_FILLED
        self.store.update_order(
            order["order_id"],
            status=status,
            filled_size=new_filled,
            avg_price=new_avg,
            finish_time=_now() if status == ORDER_FILLED else None,
        )

        quanto = self._quanto(symbol)
        fee_rate = self.store.cfg_f("maker_fee_rate" if role == "maker" else "fee_rate", 0.0005)
        notional = abs(size) * quanto * price
        fee = notional * fee_rate

        # 先幂等入账，重复则完全不动仓位/资金
        fid, created = self.store.insert_fill({
            "fill_time": _now(),
            "contract": symbol,
            "side": "buy" if size > 0 else "sell",
            "price": price,
            "size": abs(size),
            "fee": fee,
            "realised_pnl": 0.0,
            "role": role,
            "order_id": order["order_id"],
            "kind": "trade",
        })
        if not created:
            return 0.0

        realised = self._update_position(symbol, size, price, quanto=quanto,
                                         reduce_only=bool(order.get("reduce_only")))
        # 回写 realised 到该笔 fill
        try:
            conn = self.store._db()
            with self.store._lock:
                conn.execute("UPDATE fills SET realised_pnl=? WHERE id=?", (realised, fid))
                conn.commit()
        except Exception:  # noqa: BLE001
            pass
        acct = self.store.get_account()
        bal = float(acct.get("balance") or 0) - fee + realised
        self.store.update_account(balance=bal)
        self._recompute_available()
        self.recalc_equity()
        return realised

    def _update_position(self, symbol: str, size: float, price: float,
                         quanto: float = 1.0, reduce_only: bool = False) -> float:
        base_mode = self.store.cfg_s("position_mode", "single") or "single"
        # 双仓：按成交方向分 dual_long / dual_short，避免多空互相对冲
        if "dual" in str(base_mode).lower():
            # reduce_only 买单平空单 / 卖单平多单；非 reduce 则开对应方向仓
            if reduce_only:
                mode = "dual_short" if float(size) > 0 else "dual_long"
            else:
                mode = "dual_long" if float(size) > 0 else "dual_short"
            pos = {p["mode"]: p for p in self.store.get_positions(symbol)}
            if mode not in pos and base_mode in pos and float(pos.get(base_mode, {}).get("size") or 0) != 0:
                hist = pos.get(base_mode) or {}
                hs = float(hist.get("size") or 0)
                if (hs > 0) == (float(size) > 0):
                    mode = base_mode
        else:
            mode = "single"
        pos = {p["mode"]: p for p in self.store.get_positions(symbol)}
        cur = pos.get(mode) or {"size": 0.0, "entry_price": 0.0, "margin": 0.0, "realised_pnl": 0.0}
        cur_size = float(cur.get("size") or 0)
        cur_entry = float(cur.get("entry_price") or 0)
        realised = 0.0

        lev = float(cur.get("leverage") or self.store.cfg_f("leverage", 20) or 20)
        margin_mode = cur.get("margin_mode") or self.store.cfg_s("margin_mode", "isolated")

        if cur_size == 0 or (cur_size > 0 and size > 0) or (cur_size < 0 and size < 0):
            if reduce_only:
                return 0.0
            new_size = cur_size + size
            new_entry = (abs(cur_size) * cur_entry + abs(size) * price) / abs(new_size) if new_size else 0.0
            margin = abs(new_size) * quanto * price / max(lev, 1.0)
            from .risk import liquidation_price as _liq_px
            mmr = self.store.cfg_f("maintenance_margin_rate", 0.005)
            liq = _liq_px(new_entry, new_size, lev, mmr)
            self.store.upsert_position(
                symbol, mode,
                size=new_size, entry_price=new_entry, leverage=lev,
                margin=margin, margin_mode=margin_mode,
                liquidation_price=liq,
                realised_pnl=float(cur.get("realised_pnl") or 0),
            )
        else:
            close_size = min(abs(size), abs(cur_size))
            direction = 1.0 if cur_size > 0 else -1.0
            realised = (price - cur_entry) * close_size * quanto * direction
            remaining = abs(cur_size) - close_size
            if remaining <= 1e-12:
                self.store.upsert_position(
                    symbol, mode,
                    size=0.0, entry_price=0.0, leverage=lev,
                    margin=0.0, margin_mode=margin_mode,
                    realised_pnl=float(cur.get("realised_pnl") or 0) + realised,
                )
                # 仓位归零：回收**这一侧**的孤儿保护单。
                # 双向模式下同一 contract 的另一侧有自己的 SL/TP，一起撤掉就是裸仓（C-16）。
                self.store.cancel_reduce_only_price_orders(symbol, mode=mode)
            else:
                self.store.upsert_position(
                    symbol, mode,
                    size=direction * remaining, entry_price=cur_entry, leverage=lev,
                    margin=remaining * quanto * price / max(lev, 1.0), margin_mode=margin_mode,
                    realised_pnl=float(cur.get("realised_pnl") or 0) + realised,
                )
        self._recompute_available()
        return realised

    # ── 触发单扫描 ─────────────────────────────────────
    def scan_price_orders(self) -> list[dict]:
        triggered: list[dict] = []
        rows = self.store.list_price_orders(status=TRIGGER_UNTRIGGERED)
        # 先触发开仓/非 reduce_only，再触发保护单，避免无仓时保护单先被打掉
        rows.sort(key=lambda po: 1 if po.get("reduce_only") else 0)
        for po in rows:
            symbol = po["contract"]
            bid, ask = self._book(symbol)
            last = self._last(symbol)
            tp = float(po["trigger_price"])
            side = po["side"]
            rule = po.get("rule") or 0
            ttype = po.get("trigger_price_type") or "latest"
            if ttype == "mark":
                try:
                    ref = float(self.feed.get_ticker(symbol).get("mark_price") or last)
                except Exception:  # noqa: BLE001
                    ref = last
            elif ttype == "index":
                ref = last
            else:
                ref = last
            # rule: 1=价格上穿触发，2=价格下穿触发；0 时按触发价相对现价推断方向
            if rule == 1:
                hit = ref >= tp
            elif rule == 2:
                hit = ref <= tp
            else:
                # TP（高于现价）等上穿；SL（低于现价）等下穿
                hit = (ref >= tp) if tp > ref else (ref <= tp)
            if not hit:
                continue
            # 原子占用，防止双进程/双线程同时触发同一单
            if not self.store.claim_price_order(po["order_id"]):
                # P0.4：触发单被他人占用 = 重复成交尝试
                if self.alert_store is not None:
                    try:
                        self.alert_store.dup_fill(
                            po["order_id"],
                            float(po.get("size") or 0),
                            float(tp or 0),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                continue
            # po.size 已是带符号数量（负=卖平多）；side 仅用于触发方向
            body = {
                "contract": symbol,
                "size": po["size"],
                "type": po.get("order_type") or "market",
                "price": po.get("price"),
                "tif": "GTC",
                "reduce_only": bool(po.get("reduce_only")),
                "text": po.get("text") or "",
            }
            try:
                # 无仓时 reduce_only 保护单不得成交（只取消），防误开反向仓/空扣费
                if po.get("reduce_only"):
                    pos_sz = 0
                    for p in self.store.get_positions(symbol):
                        pos_sz += float(p.get("size") or 0)
                    if abs(pos_sz) < 1e-12:
                        self.store.update_price_order(
                            po["order_id"], status=TRIGGER_CANCELLED, finish_time=_now(),
                            error="reduce_only_no_position",
                        )
                        continue
                order = self.place_order(body)
                self.store.update_price_order(
                    po["order_id"], status=TRIGGER_FILLED, finish_time=_now(),
                )
                triggered.append(order)
            except PaperReject as e:
                self.store.update_price_order(
                    po["order_id"], status=TRIGGER_CANCELLED, finish_time=_now(),
                    error=str(e),
                )
        return triggered

    # ── 盈亏估值 ───────────────────────────────────────
    def recalc_equity(self) -> dict:
        acct = self.store.get_account()
        balance = float(acct.get("balance") or 0)
        unrealised = 0.0
        pos_margin = 0.0
        for p in self.store.get_positions():
            symbol = p["contract"]
            last = self._last(symbol)
            size = float(p["size"] or 0)
            entry = float(p["entry_price"] or 0)
            quanto = self._quanto(symbol, strict=False)
            if size and last and quanto:
                unrealised += (last - entry) * size * quanto
            pos_margin += abs(float(p.get("margin") or 0))
        equity = balance + unrealised
        self.store.update_account(unrealised_pnl=unrealised, position_margin=pos_margin)
        self._recompute_available()
        snap = {
            "snap_time": _now(),
            "equity": equity,
            "unrealised": unrealised,
            "realised": self.store.total_realised(),
            "drawdown": 0.0,
        }
        self.store.insert_pnl(snap)                 # 账户级（contract=None）
        # 每个有仓的币各落一条：只存合并值时「这个币到底赚没赚」永远答不出来
        # （一币亏一币赚会互相抵消）。与账户级**同频**、共用同一个 snap_time，
        # 所以事后能按时间把它们对齐（没有持仓时不写）。
        for p in self.store.get_positions():
            symbol = str(p["contract"])
            size = float(p["size"] or 0)
            entry = float(p["entry_price"] or 0)
            last = self._last(symbol)
            quanto = self._quanto(symbol, strict=False)
            upnl = (last - entry) * size * quanto if (size and last and quanto) else 0.0
            rpnl = float(p.get("realised_pnl") or 0)
            self.store.insert_pnl({
                "snap_time": snap["snap_time"],
                "equity": rpnl + upnl,
                "unrealised": upnl,
                "realised": rpnl,
                "drawdown": 0.0,
                "contract": symbol,
            })
        return snap

    def get_account_view(self) -> dict:
        acct = self.store.get_account()
        snap = self.recalc_equity()
        return {
            "id": "paper",
            "available": acct.get("available"),
            "total": snap.get("equity"),
            "position_mode": acct.get("position_mode") or "single",
            "unrealised_pnl": snap.get("unrealised"),
            "position_margin": acct.get("position_margin"),
            "order_margin": acct.get("order_margin"),
            "balance": acct.get("balance"),
            "leverage": acct.get("leverage"),
            "margin_mode": acct.get("margin_mode"),
            "equity": snap.get("equity"),
            "realised_pnl": snap.get("realised"),
            "total_fee": self.store.total_fees(),
            "total_funding": self.store.total_funding(),
        }

    def get_positions_view(self) -> list[dict]:
        out = []
        for p in self.store.get_positions():
            last = self._last(p["contract"])
            size = float(p["size"] or 0)
            entry = float(p["entry_price"] or 0)
            quanto = self._quanto(p["contract"], strict=False)
            upnl = (last - entry) * size * quanto if (last and quanto) else 0.0
            out.append({
                "contract": p["contract"],
                "size": size,
                "entry_price": entry,
                "mark_price": last,
                "leverage": p.get("leverage"),
                "margin": p.get("margin"),
                "liquidation_price": p.get("liquidation_price"),
                "unrealised_pnl": upnl,
                "mode": p.get("mode"),
                "margin_mode": p.get("margin_mode"),
            })
        return out
