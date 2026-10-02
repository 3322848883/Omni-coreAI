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
    heikin_ashi,
    linreg_channel,
    lr_ha_candles,
    ob_os_signals,
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
)

TV_TOOL_NAMES = ("tv_linreg_trendlines", "tv_rsi_yata", "tv_lr_ha_candles")

_SYM = {"type": "string", "description": "e.g. BTC_USDT"}
_TF = {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]}
_LIMIT = {"type": "integer", "minimum": 30, "maximum": 300,
          "description": "candles to fetch (default 200)"}

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

    return {"error": f"unknown tv tool {name!r}", "allowed": list(TV_TOOL_NAMES)}
