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

    **字符串必须单独认**：`bool("false")` 是 `True`。一旦某个数据源把布尔写成字符串，
    `is_reduce_only` 就恒真 —— 而它正是人格第 25 条区分「预挂保护」与「入场条件单」的
    唯一判据，判错会把入场单当保护单、或反过来撤掉真保护单。
    """
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str):
            s = v.strip().lower()
            if not s:
                continue                      # 空串视为没值，继续找下一个
            return s not in ("0", "false", "no", "none", "null")
        return bool(v)
    return None


def _side_of(size: Any) -> Optional[str]:
    """由委托张数的符号推方向（正=买=long，负=卖=short）。

    Gate 的条件单带 `direction`（long/short），paper 的扁平库行只有 `side`
    （buy/sell）—— 字段名不同，但**符号一致**。所以缺失时从 size 派生，
    比按名字映射 `side` 更可靠，也顺带覆盖了别的形状差异。
    """
    try:
        n = float(size)
    except (TypeError, ValueError):
        return None
    if n > 0:
        return "long"
    if n < 0:
        return "short"
    return None


_TIF_KIND = {"gtc": "limit", "poc": "post_only", "ioc": "ioc", "fok": "fok"}


def _order_kind(tif: Any, price: Any) -> Optional[str]:
    """从 `tif`（必要时结合 price）派生委托类型。

    **为什么需要**：Gate 的 `list_orders()` 返回里**没有 `order_type`**（实测字段
    清单里就没有这个键），只有 `tif`。AI 于是只能猜「这个 84350 是限价单还是突破单」
    —— 实测 2026-10-07 那轮，推理的前 23 行和后 10 行都在猜这件事，约占整段推理的
    五分之一。把 `tif` 翻译成它想知道的词，这段猜测就可以整段消掉。
    """
    t = str(tif or "").strip().lower()
    if t in _TIF_KIND:
        return _TIF_KIND[t]
    try:
        if price is not None and float(price) == 0:
            return "market"          # 无 tif 且价格为 0 = 市价
    except (TypeError, ValueError):
        pass
    return None


def _norm_symbols(symbols: Optional[list[str]]) -> set[str]:
    return {str(s or "").strip().upper() for s in (symbols or []) if str(s or "").strip()}


def _rows_for(rows: Any, symbols: Optional[list[str]]) -> list[dict]:
    """按 symbols 过滤账户级行；`symbols=None` = 不过滤（旧行为）。

    **为什么账户级的行必须能按币过滤**：`positions` / `open_orders` 是**账户**级的
    （多 bot 共账户时还混着别人的单）。不按币过滤就把「账户里有仓」当成「这个币有仓」，
    于是规则 16/17 会对没仓的币放行管理动作（→ `NO_POSITION` 白烧一轮），
    而规则 14 反过来永远看不到「某币有孤儿保护单」的触发条件。
    """
    if symbols is None:
        return [r for r in (rows or []) if r]
    uni = _norm_symbols(symbols)
    return [r for r in (rows or [])
            if r and str((r or {}).get("contract") or "").strip().upper() in uni]


def position_state(account: dict, symbols: Optional[list[str]] = None) -> tuple[str, str]:
    """给 AI 的持仓状态摘要：(state, note)。

    `symbols` 非 None 时只看这些币的持仓/挂单（按币状态）；None 时看整个账户（旧语义，
    即「任一币有仓」）。按币的调用点见 `collect_snapshot` 的 `position_state{symbol:…}`。

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
    pos_n = len(_rows_for(acct.get("positions"), symbols))
    oo = _rows_for(acct.get("open_orders"), symbols)
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
            "**`account.open_orders` 与 `protections` 是两类不同的单**：前者是普通挂单"
            "（限价/市价，看 `kind`），后者是交易所侧条件单。所以**在 open_orders 里"
            "出现的就一定不是条件单**，不必再去猜它是 limit 还是 stop。"
        )
    return "flat", "无持仓、无待成交入场单"


def _group_by_symbol(rows: Any) -> dict[str, list[dict]]:
    """把账户级行按 contract 分区（只做投影，**不丢行**）。

    没有 contract 的行落 `""` 键而不是被丢掉 —— 本仓反复踩到「静默丢维度」，
    分组视图漏行会让 AI 以为账户上没有这张单。
    """
    out: dict[str, list[dict]] = {}
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        out.setdefault(str(row.get("contract") or ""), []).append(row)
    return out


def _position_state_by_symbol(account: dict, symbols: Optional[list[str]]) -> tuple[dict, dict]:
    """按币的 `(states, notes)`：键 = 宇宙 ∪ 账户上出现过的 symbol。

    **为什么把宇宙外的币也列出来**：账户可能是共享的（多 bot 共账户），而规则 14/16/17
    按币取值 —— **缺键就等于无从判断**（模型只能退回账户级值，正是要修的毛病）。
    这些键的说明里显式写「不在宇宙内、只忽略」，与规则 19 一致。
    """
    universe = _norm_symbols(symbols)
    keys: list[str] = [str(s or "").strip().upper() for s in (symbols or [])
                       if str(s or "").strip()]
    for field in ("positions", "open_orders", "protections"):
        for row in (account.get(field) or []):
            if not isinstance(row, dict):
                continue
            sym = str(row.get("contract") or "").strip().upper()
            if sym and sym not in keys:
                keys.append(sym)
    states: dict[str, str] = {}
    notes: dict[str, str] = {}
    for sym in keys:
        st, note = position_state(account, [sym])
        if universe and sym not in universe:
            note = note + (
                "（**该 symbol 不在【品种宇宙】内**，不是你的 —— 只忽略、不得操作，见规则 19）"
            )
        states[sym] = st
        notes[sym] = note
    return states, notes


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
                    # `tif` 的翻译版 —— AI 想知道的就是这个词，省得它自己猜
                    "kind": _order_kind(o.get("tif"), o.get("price")),
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
                    size_v = _first(ini.get("size"), p.get("size"))
                    prot.append({
                        "contract": s,
                        "id": p.get("id"),
                        "size": size_v,
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
                        "direction": _first(p.get("direction"), ini.get("direction"),
                                            _side_of(size_v)),
                        "order_type": _first(p.get("order_type"), ini.get("type")),
                    })
            account["protections"] = prot
        except Exception as e:  # noqa: BLE001
            account["protections"] = []
            meta["degraded"].append("protections")
    # ── 按币分区（T5-a，见 docs/compose/spec/symbol-as-parameter.md [S2.4③]）──
    # 宇宙来自调用方的 symbols（唯一权威）。每行标 `in_universe`：账户可能是共享的，
    # 别人的持仓/挂单混在同一份快照里 —— 把「忽略别的 bot 的单」从提示词自律下沉为字段。
    universe = _norm_symbols(symbols)
    for rows in (account.get("positions"), account.get("open_orders"), account.get("protections")):
        for row in (rows or []):
            row["in_universe"] = str(row.get("contract") or "").strip().upper() in universe
    account["positions_by_symbol"] = _group_by_symbol(account.get("positions"))
    account["open_orders_by_symbol"] = _group_by_symbol(account.get("open_orders"))
    # 显式标注持仓状态：AI 不必从 positions/protections 的有无去猜（见 position_state）。
    # 旧单值保留一版（`*_any` = 「任一币有仓」），人格里的旧引用仍读得到。
    account["position_state_any"], account["position_state_note_any"] = position_state(account)
    account["position_state"], account["position_state_note"] = _position_state_by_symbol(
        account, symbols
    )

    return {
        "interval": interval,
        "candles_len": candles,
        "market": market,
        "account": account,
        "meta": meta,
    }
