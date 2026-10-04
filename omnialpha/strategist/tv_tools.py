"""TV Pine Script 指标工具（挂到 LLM 工具位）。

三个指标按 TradingView 原始名称命名：
  1. tv_linreg_trendlines  — Linreg & Trendlines (ParkF)
  2. tv_rsi_yata           — RSI Yata
  3. tv_lr_ha_candles      — Linear Regression Heikin Ashi Candles (B3AR_Trades)

与 tv_indicators 模块一一对应；数据走与 klines 同源的 resolve_candles。
"""
from __future__ import annotations

from typing import Any, Optional

from ..gate_client import GateClient
from .market import MarketConfig, resolve_candles
from .tv_indicators import (
    MODE_OI,
    MODE_VOLUME,
    POLARITY_BAR,
    POLARITY_PRESSURE,
    cdv_rate,
    cumulative_delta_volume,
    delta_flow_profile,
    heikin_ashi,
    heikin_ashi_from,
    linreg_channel,
    lr_ha_candles,
    ob_os_signals,
    oi_visible_range,
    rsi_base,
    rsi_bollinger,
    rsi_candles,
    rsi_histogram,
    rsi_ma,
    rsi_macd,
    rsi_smoothed,
    swing_structure,
    t3_moving_average,
    trendlines,
    volatility_bands,
    vol_oi_footprint,
)

TV_TOOL_NAMES = (
    "tv_linreg_trendlines", "tv_rsi_yata", "tv_lr_ha_candles",
    "tv_delta_flow_profile", "tv_oi_visible_range", "tv_vol_oi_footprint",
    "tv_cdv",
)

_SYM = {"type": "string", "description": "e.g. BTC_USDT"}
_TF = {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]}
_LIMIT = {"type": "integer", "minimum": 30, "maximum": 300,
          "description": "candles to fetch (default 200)"}
# 剖面类指标的窗口上限是 1500 根（对齐原版 lookback 上限），需要更长的 K 线
_LIMIT_LONG = {"type": "integer", "minimum": 30, "maximum": 1500,
               "description": "candles to fetch (default 400; must cover lookback)"}

TV_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "tv_linreg_trendlines",
            "description": (
                "Linreg & Trendlines (TV Pine, ParkF): 3-layer linear-regression channel "
                "(±1σ/±2σ/±3σ) over `length` bars plus pivot-based trendlines. "
                "Returns channel start/end per layer, slope, std_dev, pearson_r, "
                "and primary/secondary trendlines (upper from pivot highs, lower from pivot lows)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM, "tf": _TF, "limit": _LIMIT,
                    "length": {"type": "integer", "minimum": 10, "maximum": 300,
                               "description": "regression window (default 100)"},
                    "upper_mult": {"type": "number", "description": "outer band σ (default 3)"},
                    "lookback": {"type": "integer", "minimum": 3, "maximum": 50,
                                 "description": "pivot lookback for trendlines (default 25)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tv_rsi_yata",
            "description": (
                "RSI Yata (TV Pine): enhanced RSI — smoothed RSI, MA overlay, Bollinger Bands "
                "on RSI, RSI candles (OHLC), OB/OS cross signals, RSI-MA histogram, MACD on RSI, "
                "and swing structure labels (HH/HL/LH/LL)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM, "tf": _TF, "limit": _LIMIT,
                    "length": {"type": "integer", "minimum": 2, "maximum": 100,
                               "description": "RSI period (default 14)"},
                    "smooth": {"type": "integer", "minimum": 1, "maximum": 20,
                               "description": "RSI smoothing (default 3)"},
                    "ma_length": {"type": "integer", "minimum": 2, "maximum": 100,
                                  "description": "MA/BB length on RSI (default 21)"},
                    "bb_mult": {"type": "number", "description": "BB σ on RSI (default 2)"},
                    "ob_level": {"type": "number", "description": "overbought (default 70)"},
                    "os_level": {"type": "number", "description": "oversold (default 30)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tv_lr_ha_candles",
            "description": (
                "LR HA Candles (TV Pine, B3AR_Trades): Linear-Regression Heikin-Ashi candles "
                "(LR applied to HA open/high/low/close) + T3 moving average + ATR volatility bands."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM, "tf": _TF, "limit": _LIMIT,
                    "length": {"type": "integer", "minimum": 2, "maximum": 100,
                               "description": "LR window for HA candles (default 9)"},
                    "t3_length": {"type": "integer", "minimum": 2, "maximum": 50,
                                  "description": "T3 length (default 5)"},
                    "t3_alpha": {"type": "number", "description": "T3 alpha (default 0.7)"},
                    "vb_length": {"type": "integer", "minimum": 5, "maximum": 100,
                                  "description": "volatility-band length (default 20)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tv_delta_flow_profile",
            "description": (
                "Delta Flow Profile (TV Pine, LuxAlgo): money-flow profile over the last "
                "`lookback` bars — per price level the money flow (volume × overlap ratio × "
                "level mid-price) and the delta (buy − sell), plus the POC and its bar-by-bar "
                "migration path (Developing PoC). Pure OHLCV; no external data source."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM, "tf": _TF, "limit": _LIMIT_LONG,
                    "lookback": {"type": "integer", "minimum": 10, "maximum": 1500,
                                 "description": "profile window in bars (default 360)"},
                    "rows": {"type": "integer", "minimum": 10, "maximum": 125,
                             "description": "price rows (default 25)"},
                    "polarity": {"type": "string",
                                 "enum": [POLARITY_BAR, POLARITY_PRESSURE],
                                 "description": ("buy/sell split: bar_polarity = close>open, "
                                                 "bar_pressure = (close-low)>(high-close) "
                                                 "(default bar_polarity)")},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tv_oi_visible_range",
            "description": (
                "OI Visible Range (TV Pine, Kioseff Trading): open-interest quadrants by price "
                "level over `limit` bars — each bar's open-interest change is spread across the "
                "price levels it spans, classified into 4 quadrants (price up/down × OI up/down = "
                "buyers entered / sellers exited / sellers entered / buyers exited). Returns each "
                "quadrant's POC, value area, share, plus per-level and summed buckets. "
                "Needs Gate /contract_stats for the OI series."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM, "tf": _TF, "limit": _LIMIT_LONG,
                    "rows": {"type": "integer", "minimum": 7, "maximum": 2000,
                             "description": "price levels (default 20)"},
                    "va_pct": {"type": "number", "minimum": 5, "maximum": 95,
                               "description": "value-area percentage (default 70)"},
                    "delta_rows": {"type": "integer", "minimum": 2, "maximum": 500,
                                   "description": "summed-OI buckets (default 50, capped at 125)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tv_vol_oi_footprint",
            "description": (
                "Volume / Open Interest Footprint (TV Pine, Leviathan Capital): per price level "
                "footprint over `limit` bars, splitting volume into buy/sell by candle geometry "
                "(body weight 1, wicks weight 2, wick volume split half green / half red). "
                "mode=volume uses OHLCV only; mode=oi uses the open-interest change instead "
                "(body only, no wicks). Returns per-level green/red/delta/total, totals, POC and "
                "positive-delta price levels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM, "tf": _TF, "limit": _LIMIT_LONG,
                    "resolution": {"type": "integer", "minimum": 5, "maximum": 200,
                                   "description": "price rows (default 20)"},
                    "mode": {"type": "string", "enum": [MODE_VOLUME, MODE_OI],
                             "description": "volume (default) or oi"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tv_cdv",
            "description": (
                "Cumulative Delta Volume (TV Pine, LonesomeTheBlue): running sum of per-bar "
                "delta, where delta is ESTIMATED from candle geometry (body size vs wicks) — "
                "the standard approximation when tick-level buy/sell direction is unavailable. "
                "Returns the CDV series tail, per-bar delta tail, optional Heikin-Ashi CDV "
                "candles, and optional SMA/EMA overlays."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM, "tf": _TF, "limit": _LIMIT,
                    "ha": {"type": "boolean",
                           "description": "apply Heikin-Ashi to the CDV candles (default false)"},
                    "sma1": {"type": "integer", "minimum": 0, "maximum": 400,
                             "description": "SMA #1 length on CDV (0 = off)"},
                    "sma2": {"type": "integer", "minimum": 0, "maximum": 400,
                             "description": "SMA #2 length on CDV (0 = off)"},
                    "ema1": {"type": "integer", "minimum": 0, "maximum": 400,
                             "description": "EMA #1 length on CDV (0 = off)"},
                    "ema2": {"type": "integer", "minimum": 0, "maximum": 400,
                             "description": "EMA #2 length on CDV (0 = off)"},
                },
                "required": ["symbol"],
            },
        },
    },
]


def _rows(client, args: dict, env: str, bot_root, market_cfg: Optional[MarketConfig]):
    sym = str(args.get("symbol") or args.get("sym") or "BTC_USDT")
    tf = str(args.get("tf") or args.get("interval") or "15m").lower()
    lim = max(30, min(int(args.get("limit") or 200), 300))
    res = resolve_candles(client, sym, tf, lim, market_cfg=market_cfg, env=env, bot_root=bot_root)
    rows = list(res.rows or [])
    o = [float(r["o"]) for r in rows]
    h = [float(r["h"]) for r in rows]
    l = [float(r["l"]) for r in rows]
    c = [float(r["c"]) for r in rows]
    return sym, tf, len(rows), o, h, l, c, getattr(res, "source", None)


def _tail(seq: list, n: int = 5) -> list:
    """取最后 n 个有效值（None 跳过）。"""
    out = [x for x in seq if x is not None]
    return out[-n:]


def _round(seq: list, digits: int = 6, n: int = 5) -> list:
    return [None if x is None else round(float(x), digits) for x in _tail(seq, n)]


def _ohlcv(client, args: dict, env: str, bot_root,
           market_cfg: Optional[MarketConfig], *, max_limit: int = 300,
           default_limit: int = 200):
    """取 K 线并返回**原始 rows**。

    剖面类指标要用到 `v` 和 `t`，而 `_rows` 只解包 o/h/l/c —— 故另开一个入口。
    """
    sym = str(args.get("symbol") or args.get("sym") or "BTC_USDT")
    tf = str(args.get("tf") or args.get("interval") or "15m").lower()
    lim = max(30, min(int(args.get("limit") or default_limit), max_limit))
    res = resolve_candles(client, sym, tf, lim, market_cfg=market_cfg,
                          env=env, bot_root=bot_root)
    return sym, tf, list(res.rows or []), getattr(res, "source", None)


def _fetch_oi_series(client, sym: str, tf: str, limit: int) -> dict:
    """`contract_stats` 按 interval 拉 OI 序列 → `{bar_time: open_interest}`。

    该接口的 `interval` 与 K 线周期同构（1m/5m/15m/30m/1h/4h/1d），返回的是
    **按周期聚合的历史序列**而非单点快照，故能与 klines 按 bar 时间直接对齐。
    取不到时返回空 dict，由调用方降级成明确的错误信息。
    """
    try:
        stats = client.get_contract_stats(sym, limit=int(limit), interval=tf)
    except Exception:  # noqa: BLE001 — 网络/接口异常统一降级，不炸整轮
        return {}
    out: dict = {}
    for x in stats or []:
        if not isinstance(x, dict):
            continue
        t = x.get("time")
        oi = x.get("open_interest")
        if t is None or oi is None:
            continue
        try:
            out[int(t)] = float(oi)
        except (TypeError, ValueError):
            continue
    return out


def _align_by_time(rows: list, oi_map: dict) -> list:
    """按 bar 时间取交集 → `[(row, oi), ...]`（按时间升序）。"""
    pairs = []
    for r in rows:
        t = r.get("t")
        if t is None:
            continue
        try:
            ti = int(t)
        except (TypeError, ValueError):
            continue
        if ti in oi_map:
            pairs.append((r, oi_map[ti]))
    pairs.sort(key=lambda p: int(p[0]["t"]))
    return pairs


def run_tv_tool(client: GateClient, name: str, args: dict,
                *, env: str = "live", bot_root=None,
                market_cfg: Optional[MarketConfig] = None) -> Any:
    """执行 TV 指标工具。"""
    args = dict(args or {})

    if name == "tv_linreg_trendlines":
        sym, tf, n, o, h, l, c, src = _rows(client, args, env, bot_root, market_cfg)
        if n < 10:
            return {"error": f"not enough candles ({n})", "symbol": sym, "tf": tf}
        length = int(args.get("length") or 100)
        upper_mult = float(args.get("upper_mult") or 3.0)
        lookback = int(args.get("lookback") or 25)
        ch = linreg_channel(c, h, l, length=length,
                            upper_mult=upper_mult, lower_mult=upper_mult)
        tl = trendlines(h, l, c, o, lookback=lookback)
        return {
            "symbol": sym, "tf": tf, "bars": n, "source": src,
            "params": {"length": length, "upper_mult": upper_mult, "lookback": lookback},
            "channel": {
                "base": ch.get("base"),
                "layers": ch.get("layers"),
                "slope": None if ch.get("slope") is None else round(ch["slope"], 8),
                "std_dev": None if ch.get("std_dev") is None else round(ch["std_dev"], 4),
                "pearson_r": None if ch.get("pearson_r") is None else round(ch["pearson_r"], 6),
            },
            "trendlines": tl,
            "last_close": c[-1],
        }

    if name == "tv_rsi_yata":
        sym, tf, n, o, h, l, c, src = _rows(client, args, env, bot_root, market_cfg)
        rsi_len = int(args.get("length") or 14)
        if n < rsi_len + 2:
            return {"error": f"not enough candles ({n}) for RSI{rsi_len}", "symbol": sym, "tf": tf}
        smooth = int(args.get("smooth") or 3)
        ma_len = int(args.get("ma_length") or 21)
        bb_mult = float(args.get("bb_mult") or 2.0)
        ob = float(args.get("ob_level") or 70)
        os_lv = float(args.get("os_level") or 30)

        rsi_v = rsi_base(c, rsi_len)
        sm = rsi_smoothed(rsi_v, smooth)
        ma = rsi_ma(rsi_v, ma_len, "SMA")
        bb = rsi_bollinger(rsi_v, ma_len, bb_mult)
        candles = rsi_candles(rsi_v, rsi_len)
        sig = ob_os_signals(rsi_v, ob, os_lv)
        hist = rsi_histogram(rsi_v, ma)
        mac = rsi_macd(rsi_v, 12, 26, 9)
        struct = swing_structure(rsi_v, 3, 3)

        # 摆动结构标签（非空）
        labels = [{"bar": i, "rsi": round(float(rsi_v[i]), 4), "tag": struct[i]}
                  for i in range(len(struct)) if struct[i]]

        return {
            "symbol": sym, "tf": tf, "bars": n, "source": src,
            "params": {"length": rsi_len, "smooth": smooth, "ma_length": ma_len,
                       "bb_mult": bb_mult, "ob": ob, "os": os_lv},
            "rsi_last": None if rsi_v[-1] is None else round(rsi_v[-1], 4),
            "rsi_series_tail": _round(rsi_v, 4),
            "smoothed_tail": _round(sm, 4),
            "ma_tail": _round(ma, 4),
            "bollinger_last": {
                "ma": None if bb["ma"][-1] is None else round(bb["ma"][-1], 4),
                "upper": None if bb["upper"][-1] is None else round(bb["upper"][-1], 4),
                "lower": None if bb["lower"][-1] is None else round(bb["lower"][-1], 4),
            },
            "rsi_candles_last": {
                k: (None if candles[k][-1] is None else round(candles[k][-1], 4))
                for k in ("open", "high", "low", "close")
            },
            "histogram_tail": _round(hist, 4),
            "macd_hist_tail": _round(mac["hist"], 4),
            "ob_os_recent": [
                {"bar": i, "tag": "OB" if sig["overbought"][i] else "OS"}
                for i in range(len(rsi_v))
                if sig["overbought"][i] or sig["oversold"][i]
            ][-5:],
            "structure_labels": labels[-8:],
        }

    if name == "tv_lr_ha_candles":
        sym, tf, n, o, h, l, c, src = _rows(client, args, env, bot_root, market_cfg)
        length = int(args.get("length") or 9)
        if n < length + 2:
            return {"error": f"not enough candles ({n}) for LR{length}", "symbol": sym, "tf": tf}
        t3_len = int(args.get("t3_length") or 5)
        t3_alpha = float(args.get("t3_alpha") or 0.7)
        vb_len = int(args.get("vb_length") or 20)

        ha = heikin_ashi(o, h, l, c)
        lrha = lr_ha_candles(o, h, l, c, length=length)
        t3 = t3_moving_average(c, t3_len, t3_alpha)
        vb = volatility_bands(h, l, c, length=vb_len)

        return {
            "symbol": sym, "tf": tf, "bars": n, "source": src,
            "params": {"length": length, "t3_length": t3_len, "t3_alpha": t3_alpha,
                       "vb_length": vb_len},
            "heikin_ashi_last": {
                k: (None if ha[k][-1] is None else round(ha[k][-1], 6))
                for k in ("open", "high", "low", "close")
            },
            "lr_ha_last": {
                k: (None if lrha[k][-1] is None else round(lrha[k][-1], 6))
                for k in ("open", "high", "low", "close")
            },
            "t3_last": None if t3[-1] is None else round(t3[-1], 6),
            "volatility_bands_last": {
                "basis": None if vb["basis"][-1] is None else round(vb["basis"][-1], 6),
                "upper_inner": None if vb["upper_inner"][-1] is None else round(vb["upper_inner"][-1], 6),
                "lower_inner": None if vb["lower_inner"][-1] is None else round(vb["lower_inner"][-1], 6),
                "upper_outer": None if vb["upper_outer"][-1] is None else round(vb["upper_outer"][-1], 6),
                "lower_outer": None if vb["lower_outer"][-1] is None else round(vb["lower_outer"][-1], 6),
            },
            "last_close": c[-1],
        }

    if name == "tv_delta_flow_profile":
        sym, tf, rows, src = _ohlcv(client, args, env, bot_root, market_cfg,
                                    max_limit=1500, default_limit=400)
        n = len(rows)
        if n < 10:
            return {"error": f"not enough candles ({n})", "symbol": sym, "tf": tf}
        lookback = max(10, min(int(args.get("lookback") or 360), 1500))
        row_count = max(10, min(int(args.get("rows") or 25), 125))
        polarity = str(args.get("polarity") or POLARITY_BAR)
        if polarity not in (POLARITY_BAR, POLARITY_PRESSURE):
            return {"error": f"unknown polarity {polarity!r}",
                    "allowed": [POLARITY_BAR, POLARITY_PRESSURE]}
        r = delta_flow_profile(
            [float(x["o"]) for x in rows], [float(x["h"]) for x in rows],
            [float(x["l"]) for x in rows], [float(x["c"]) for x in rows],
            [float(x.get("v") or 0.0) for x in rows],
            lookback=lookback, rows=row_count, polarity=polarity)
        if "error" in r:
            return {**r, "symbol": sym, "tf": tf}
        return {"symbol": sym, "tf": tf, "bars": n, "source": src, **r}

    if name == "tv_oi_visible_range":
        sym, tf, rows, src = _ohlcv(client, args, env, bot_root, market_cfg,
                                    max_limit=1500, default_limit=400)
        oi_map = _fetch_oi_series(client, sym, tf, max(len(rows), 400))
        pairs = _align_by_time(rows, oi_map)
        if len(pairs) < 3:
            return {"error": "not enough bars aligned between klines and contract_stats",
                    "symbol": sym, "tf": tf,
                    "klines": len(rows), "oi_points": len(oi_map)}
        row_count = max(7, min(int(args.get("rows") or 20), 2000))
        va_pct = max(5.0, min(float(args.get("va_pct") or 70.0), 95.0))
        delta_rows = max(2, min(int(args.get("delta_rows") or 50), 500))
        r = oi_visible_range(
            [float(x["h"]) for x, _ in pairs], [float(x["l"]) for x, _ in pairs],
            [float(x["o"]) for x, _ in pairs], [float(x["c"]) for x, _ in pairs],
            [oi for _, oi in pairs],
            rows=row_count, va_pct=va_pct, delta_rows=delta_rows)
        if "error" in r:
            return {**r, "symbol": sym, "tf": tf}
        return {"symbol": sym, "tf": tf, "source": src,
                "aligned_bars": len(pairs), **r}

    if name == "tv_vol_oi_footprint":
        mode = str(args.get("mode") or MODE_VOLUME).lower()
        if mode not in (MODE_VOLUME, MODE_OI):
            return {"error": f"unknown mode {mode!r}",
                    "allowed": [MODE_VOLUME, MODE_OI]}
        sym, tf, rows, src = _ohlcv(client, args, env, bot_root, market_cfg,
                                    max_limit=1500, default_limit=400)
        resolution = max(5, min(int(args.get("resolution") or 20), 200))
        oi_values = None
        if mode == MODE_OI:
            oi_map = _fetch_oi_series(client, sym, tf, max(len(rows), 400))
            pairs = _align_by_time(rows, oi_map)
            if len(pairs) < 3:
                return {"error": "not enough bars aligned between klines and contract_stats",
                        "symbol": sym, "tf": tf,
                        "klines": len(rows), "oi_points": len(oi_map)}
            rows = [x for x, _ in pairs]
            oi_values = [oi for _, oi in pairs]
        n = len(rows)
        if n < 3:
            return {"error": f"not enough candles ({n})", "symbol": sym, "tf": tf}
        r = vol_oi_footprint(
            [float(x["h"]) for x in rows], [float(x["l"]) for x in rows],
            [float(x["o"]) for x in rows], [float(x["c"]) for x in rows],
            [float(x.get("v") or 0.0) for x in rows],
            resolution=resolution, mode=mode, oi_values=oi_values)
        if "error" in r:
            return {**r, "symbol": sym, "tf": tf}
        return {"symbol": sym, "tf": tf, "bars": n, "source": src, **r}

    if name == "tv_cdv":
        sym, tf, rows, src = _ohlcv(client, args, env, bot_root, market_cfg,
                                    max_limit=1500, default_limit=400)
        n = len(rows)
        if n < 2:
            return {"error": f"not enough candles ({n})", "symbol": sym, "tf": tf}
        o = [float(x["o"]) for x in rows]
        h = [float(x["h"]) for x in rows]
        l = [float(x["l"]) for x in rows]
        c = [float(x["c"]) for x in rows]
        v = [float(x.get("v") or 0.0) for x in rows]
        r = cumulative_delta_volume(o, h, l, c, v)
        cdv, delta = r["cdv"], r["delta"]

        out = {
            "symbol": sym, "tf": tf, "bars": n, "source": src,
            "cdv_last": round(cdv[-1], 4),
            "delta_last": round(delta[-1], 4),
            "cdv_tail": [round(x, 4) for x in cdv[-10:]],
            "delta_tail": [round(x, 4) for x in delta[-10:]],
        }

        # 均线作用于 CDV close；启用 HA 时作用于 HA close（对齐原版 c_）
        series = cdv
        if bool(args.get("ha")):
            ha = heikin_ashi_from(r["cdv_open"], r["cdv_high"], r["cdv_low"], r["cdv_close"])
            out["ha_last"] = {
                k: (None if ha[k][-1] is None else round(ha[k][-1], 4))
                for k in ("open", "high", "low", "close")
            }
            series = [0.0 if x is None else float(x) for x in ha["close"]]

        for key, kind in (("sma1", "sma"), ("sma2", "sma"),
                          ("ema1", "ema"), ("ema2", "ema")):
            length = int(args.get(key) or 0)
            if length <= 0 or len(series) < 2:
                continue
            if kind == "sma":
                if len(series) < length:
                    continue
                out[key + "_last"] = round(sum(series[-length:]) / length, 4)
            else:
                k = 2.0 / (length + 1.0)
                e = series[0]
                for x in series[1:]:
                    e = x * k + e * (1.0 - k)
                out[key + "_last"] = round(e, 4)
        return out

    return {"error": f"unknown tv tool {name!r}", "allowed": list(TV_TOOL_NAMES)}
