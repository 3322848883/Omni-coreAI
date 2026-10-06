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


def _first(*vals: Any) -> Any:
    """返回第一个非 None 的值（0 / 空串也算有值）。"""
    for v in vals:
        if v is not None:
            return v
    return None


def _flag(*vals: Any) -> Optional[bool]:
    """取第一个非 None 的布尔标记并归一化成 bool。

    两种形状的取值不一样：Gate 用 `true`/`false`，paper 的 `reduce_only` 是 `0`/`1`。
    原样透传会让 AI 看到混着的两种写法，还要自己再猜一层。
    """
    for v in vals:
        if v is not None:
            return bool(v)
    return None


def position_state(account: dict) -> tuple[str, str]:
    """给 AI 的持仓状态摘要：(state, note)。

    **为什么需要**：执行器挂入场单后 1–3 秒就把 TP/SL 一起挂上（不等成交），所以
    「无持仓 + 有待成交入场单 + 有保护单」是**常态**。但 AI 看到 `protections` 里有单、
    `positions` 里没有，很容易误读成「有持仓可管」→ 发 `modify_tp_sl` → `NO_POSITION`。
    实盘 2026-10-02 07:59 那轮就是这么失败的（07:44 挂的 `t-brk` 19 张未成交）。

    所以在 snapshot 里显式标注状态，让 AI 不必从 `positions`/`protections` 的存在与否去猜。

    **为什么还要有 `unknown`**：账户接口失败时 `positions` 同样是**空列表**，与
    「真的没持仓」长得一模一样。把它报成 `flat` 会让 AI 在规则 14（孤儿保护单必须撤）下
    撤掉**真实持仓**的止损 —— 实测 2026-10-04 那轮就发出了 `cancel_price_all`，
    而账户里 `BTC_USDT size=2366` 的持仓还在（只因当时没有 `run` 进程消费 inbox 才没出事）。
    「取不到」和「取到空」必须分开报。
    """
    acct = account or {}
    if acct.get("error"):
        return "unknown", (
            "**持仓数据不可用**（账户接口取数失败）：`positions` 为空**不代表**无持仓。"
            "不得据此判定无持仓；不得发 modify_tp_sl / close_* / reduce_* / flatten；"
            "**尤其不得撤销 tp/sl 保护单**（规则 14 在此状态下不适用）。只 hold 并说明。"
        )
    pos_n = len([p for p in acct.get("positions") or [] if p])
    oo = acct.get("open_orders") or []
    pend_n = len([
        o for o in oo
        if str((o or {}).get("status") or "").lower() in ("open", "partially_filled")
    ])
    if pos_n:
        return "position_open", (
            "有持仓；protections 里的单属于该持仓，可用 modify_tp_sl 调整 TP/SL"
        )
    if pend_n:
        return "entry_pending", (
            "**无持仓**：只有未成交的入场委托。protections 里的单是随入场单预挂的，"
            "成交后才成为该持仓的保护 —— 此时**不要发 modify_tp_sl**（会 NO_POSITION），"
            "也不要以为已有仓位。"
            "**判据**：protections 里 `is_reduce_only: true` 的是预挂保护单，"
            "`is_reduce_only: false` 的是入场条件单（stop_entry）本身；"
            "**预挂保护单不是孤儿单，不得撤销**（它们要等入场成交才生效）。"
        )
    return "flat", "无持仓、无待成交入场单"


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
            rows = attach_indicators(list(result.rows), cfg.indicators)
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
                entry["indicators"] = latest_indicators(rows, cfg.indicators)

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
                        xrows = attach_indicators(list(xres.rows), cfg.indicators)
                        entry["tf"][tf] = {
                            "candles": xrows[-n_extra:] if xrows else [],
                            "indicators": latest_indicators(xrows, cfg.indicators) if xrows else {},
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
        # REST open orders + TP/SL (authoritative for position management)
        try:
            oos = []
            for o in (client.list_orders() or []):
                oos.append({
                    "contract": o.get("contract"),
                    "id": o.get("id"),
                    "size": o.get("size"),
                    "price": o.get("price"),
                    "left": o.get("left"),
                    "status": o.get("status"),
                    "text": o.get("text"),
                    # 普通挂单走 `list_orders()`，Gate 在这里**不给** `order_type`
                    # （实测字段清单里没有），能区分委托类型的只有 `tif`。
                    "tif": o.get("tif"),
                    "is_reduce_only": _flag(o.get("is_reduce_only"), o.get("reduce_only")),
                })
            account["open_orders"] = oos
        except Exception as e:  # noqa: BLE001
            account["open_orders"] = []
            meta["degraded"].append("open_orders")
        try:
            prot = []
            syms = set(symbols) | {p.get("contract") for p in account.get("positions") or []}
            for s in sorted(x for x in syms if x):
                for p in (client.list_price_orders(s) or []):
                    # 条件单有两种形状，必须都认：
                    #   Gate  → 字段嵌在 initial / trigger 里
                    #   paper → `_price_order_view` 是**扁平库行**（`dict(po)` + id），
                    #           字段直接在顶层（trigger_price / size / rule）
                    # 少了这层 fallback，模拟盘上 trigger_price/size/rule 全是 null ——
                    # AI 看不到当前 TP/SL 设在哪、多大量，`modify_tp_sl` 变成半盲改单。
                    # 实测 2026-10-04 模型自己指出「their trigger prices are null in the data」。
                    ini = p.get("initial") or {}
                    trg = p.get("trigger") or {}
                    prot.append({
                        "contract": s,
                        "id": p.get("id"),
                        "size": _first(ini.get("size"), p.get("size")),
                        "trigger_price": _first(trg.get("price"), p.get("trigger_price")),
                        "rule": _first(trg.get("rule"), p.get("rule")),
                        "text": _first(ini.get("text"), p.get("text")),
                        "status": p.get("status"),
                        # **入场条件单与预挂保护单都落在这个数组里** —— 执行器挂
                        # stop_entry 时会把 TP/SL 一起挂上（`hang_mode: simultaneous`，
                        # 成交即生效、零裸仓窗口，是刻意设计）。两者的 6 个老字段
                        # 形状完全一样，AI 只能靠 `text` 后缀猜。
                        # 实测 2026-10-06：AI 花了上千字猜「85850 那个单是 stop 还是
                        # limit」，而 trades.jsonl 里 order_type 写得清清楚楚。
                        # `is_reduce_only` 是唯一可靠的判据：true = 保护单，false = 入场单。
                        "is_reduce_only": _flag(
                            ini.get("is_reduce_only"), ini.get("reduce_only"),
                            p.get("is_reduce_only"), p.get("reduce_only"),
                        ),
                        "direction": _first(p.get("direction"), ini.get("direction")),
                        "order_type": _first(p.get("order_type"), ini.get("type")),
                    })
            account["protections"] = prot
        except Exception as e:  # noqa: BLE001
            account["protections"] = []
            meta["degraded"].append("protections")
    # 显式标注持仓状态：AI 不必从 positions/protections 的有无去猜（见 position_state）
    account["position_state"], account["position_state_note"] = position_state(account)

    return {
        "interval": interval,
        "candles_len": candles,
        "market": market,
        "account": account,
        "meta": meta,
    }
