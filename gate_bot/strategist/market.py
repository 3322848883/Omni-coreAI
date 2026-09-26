"""Hybrid market data: local pa-data-source kline.db + REST fallback."""
from __future__ import annotations

import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..gate_client import GateClient

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

ALL_TIMEFRAMES = tuple(INTERVAL_SECONDS.keys())

# Broad default set when indicators: all
ALL_INDICATORS = (
    "ema9", "ema20", "ema50", "ema200",
    "sma20", "sma50",
    "rsi7", "rsi14",
    "atr14",
    "macd", "macd_dea", "macd_hist",
    "boll20", "boll_upper", "boll_middle", "boll_lower",
)


@dataclass
class MarketConfig:
    mode: str = "rest_only"  # hybrid | rest_only | local_only
    pa_data_root: Optional[str] = None
    db: str = "kline.db"
    # exchange id for multi-venue: gate|binance|okx|bybit|bitget|hyperliquid
    exchange: str = "gate"
    stale_factor: float = 2.0
    health_url: Optional[str] = None
    indicators: list[str] = field(default_factory=lambda: ["ema20", "ema50", "atr14", "rsi14"])
    # additional candle timeframes for multi-TF analysis (primary stays `interval`/timeframe)
    extra_timeframes: list[str] = field(default_factory=list)
    extra_candles: int = 20
    # P1 realtime refresh blocks (always REST): ticker includes funding/mark/index/24h
    refresh: list[str] = field(default_factory=lambda: ["ticker", "stats", "orderbook"])

    def __post_init__(self) -> None:
        mode = str(self.mode or "rest_only").strip().lower()
        if mode not in ("hybrid", "rest_only", "local_only"):
            raise ValueError(f"market.mode must be hybrid|rest_only|local_only, got {self.mode!r}")
        self.mode = mode
        self.db = str(self.db or "kline.db")
        if self.stale_factor is None:
            self.stale_factor = 2.0
        else:
            self.stale_factor = float(self.stale_factor)
        self.indicators = [str(x) for x in (self.indicators or [])]
        if self.indicators and any(str(x).lower() in ("all", "*") for x in self.indicators):
            self.indicators = list(ALL_INDICATORS)
        if self.indicators:
            from .indicators import IndicatorNameError, parse_indicator_name

            for name in self.indicators:
                try:
                    parse_indicator_name(name)
                except IndicatorNameError as e:
                    raise ValueError(str(e)) from e
        self.extra_timeframes = [str(x).lower() for x in (self.extra_timeframes or []) if x]
        if any(tf in ("all", "*") for tf in self.extra_timeframes) or self.extra_timeframes == ["all"]:
            self.extra_timeframes = list(ALL_TIMEFRAMES)
        for tf in self.extra_timeframes:
            if tf not in INTERVAL_SECONDS:
                raise ValueError(f"market.extra_timeframes unknown interval {tf!r}")
        self.extra_candles = max(5, int(self.extra_candles or 20))
        allowed = {"ticker", "stats", "orderbook", "last"}
        self.refresh = [str(x).lower() for x in (self.refresh or []) if str(x).lower() in allowed]


@dataclass
class CandleResult:
    rows: list[dict[str, Any]]
    source: str  # local | exchange
    stale: bool = False
    degraded: list[str] = field(default_factory=list)
    error: Optional[str] = None


def interval_seconds(interval: str) -> int:
    return INTERVAL_SECONDS.get(str(interval).lower(), 900)


def resolve_pa_data_root(market_cfg: Optional[MarketConfig] = None, bot_root: Optional[Path] = None) -> Optional[Path]:
    env = os.environ.get("GATE_BOT_PA_DATA")
    if env:
        return Path(env).expanduser().resolve()
    if market_cfg and market_cfg.pa_data_root:
        p = Path(market_cfg.pa_data_root).expanduser()
        if not p.is_absolute():
            base = bot_root or Path.cwd()
            p = (base / p).resolve()
        return p
    if bot_root:
        # monorepo default: <root>/pa-data-source/data
        return (bot_root / "pa-data-source" / "data").resolve()
    return None


def resolve_db_path(
    env: str,
    market_cfg: Optional[MarketConfig] = None,
    bot_root: Optional[Path] = None,
) -> Optional[Path]:
    root = resolve_pa_data_root(market_cfg, bot_root)
    if root is None:
        return None
    name = (market_cfg.db if market_cfg else "kline.db") or "kline.db"
    ex = (getattr(market_cfg, "exchange", "gate") or "gate").lower() if market_cfg else "gate"
    if name == "kline.db" and ex and ex != "gate":
        name = f"kline_{ex}.db"
    if str(env).lower() == "testnet":
        if name == "kline.db":
            name = "kline_testnet.db"
        elif not name.endswith("_testnet.db"):
            name = name.replace(".db", "_testnet.db")
    return root / name


def health_ok(url: Optional[str], timeout: float = 2.0) -> bool:
    """True when health endpoint is absent (no gate) or returns HTTP 200."""
    if not url:
        return True
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status) == 200
    except Exception:  # noqa: BLE001
        return False


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_local_candles(db_path: Optional[Path], symbol: str, interval: str, limit: int) -> Optional[list[dict[str, Any]]]:
    if not db_path or not Path(db_path).exists():
        return None
    try:
        from contracts.kline_schema import validate_kline_schema

        validate_kline_schema(Path(db_path))
    except Exception:  # noqa: BLE001 — contract miss → treat as unreadable, fall back REST
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            rows = conn.execute(
                "SELECT t,o,h,l,c,v,sum,ema20,atr14 FROM kline "
                "WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?",
                (symbol, interval, int(limit)),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    out: list[dict[str, Any]] = []
    for r in reversed(rows):
        out.append(
            {
                "t": int(r[0]),
                "o": _to_float(r[1]),
                "h": _to_float(r[2]),
                "l": _to_float(r[3]),
                "c": _to_float(r[4]),
                "v": _to_float(r[5]),
                "sum": _to_float(r[6]),
                "ema20": _to_float(r[7]),
                "atr14": _to_float(r[8]),
            }
        )
    return out


def fetch_rest_candles(client: GateClient, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    raw = client.public_get(
        "/api/v4/futures/usdt/candlesticks",
        f"contract={urllib.parse.quote(symbol)}&interval={interval}&limit={int(limit)}",
    )
    rows: list[dict[str, Any]] = []
    for r in raw or []:
        if isinstance(r, (list, tuple)) and len(r) >= 6:
            rows.append(
                {
                    "t": int(r[0]),
                    "v": _to_float(r[1]),
                    "c": _to_float(r[2]),
                    "h": _to_float(r[3]),
                    "l": _to_float(r[4]),
                    "o": _to_float(r[5]),
                    "sum": _to_float(r[6]) if len(r) > 6 else None,
                    "ema20": None,
                    "atr14": None,
                }
            )
        elif isinstance(r, dict):
            rows.append(
                {
                    "t": int(r.get("t") or 0),
                    "o": _to_float(r.get("o")),
                    "h": _to_float(r.get("h")),
                    "l": _to_float(r.get("l")),
                    "c": _to_float(r.get("c")),
                    "v": _to_float(r.get("v")),
                    "sum": _to_float(r.get("sum")),
                    "ema20": _to_float(r.get("ema20")),
                    "atr14": _to_float(r.get("atr14")),
                }
            )
    rows.sort(key=lambda x: x["t"])
    return rows[-int(limit) :]


def _to_epoch_seconds(ts: float) -> float:
    # pa/Gate candlestick t is seconds; tolerate ms epoch.
    return ts / 1000.0 if ts > 1e12 else ts


def is_stale(
    rows: list[dict[str, Any]],
    interval: str,
    stale_factor: float = 2.0,
    now: Optional[float] = None,
) -> bool:
    if not rows:
        return True
    ts = now if now is not None else time.time()
    last_t = _to_epoch_seconds(float(rows[-1].get("t") or 0))
    return (ts - last_t) > float(stale_factor) * interval_seconds(interval)


def resolve_candles(
    client: GateClient,
    symbol: str,
    interval: str,
    limit: int,
    market_cfg: Optional[MarketConfig] = None,
    env: str = "live",
    bot_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> CandleResult:
    cfg = market_cfg or MarketConfig()
    degraded: list[str] = []
    local_rows: Optional[list[dict[str, Any]]] = None
    use_local_gate = True

    if cfg.mode in ("hybrid", "local_only"):
        if cfg.health_url and not health_ok(cfg.health_url):
            use_local_gate = False
            degraded.append("health_untrusted")
        db_path = resolve_db_path(env, cfg, bot_root)
        if cfg.mode == "hybrid" and db_path is None:
            degraded.append("local_db_path")
        else:
            local_rows = load_local_candles(db_path, symbol, interval, limit)
            if local_rows is None and cfg.mode == "hybrid":
                degraded.append("local_db")
            if local_rows is not None and not use_local_gate and cfg.mode == "hybrid":
                local_rows = None

    if cfg.mode == "rest_only":
        try:
            rows = fetch_rest_candles(client, symbol, interval, limit)
            return CandleResult(rows=rows, source="exchange", stale=False, degraded=degraded)
        except Exception as e:  # noqa: BLE001
            return CandleResult(rows=[], source="exchange", stale=True, degraded=degraded, error=str(e))

    if cfg.mode == "local_only":
        if local_rows is None:
            return CandleResult(rows=[], source="local", stale=True, degraded=degraded, error="local_db_empty")
        stale = is_stale(local_rows, interval, cfg.stale_factor, now=now)
        if stale:
            degraded.append("candles_stale")
        return CandleResult(rows=local_rows, source="local", stale=stale, degraded=degraded)

    # hybrid
    if local_rows is not None:
        stale = is_stale(local_rows, interval, cfg.stale_factor, now=now)
        if not stale:
            return CandleResult(rows=local_rows, source="local", stale=False, degraded=degraded)
        degraded.append("candles_stale")
    try:
        rows = fetch_rest_candles(client, symbol, interval, limit)
        src = "exchange"
        stale = is_stale(rows, interval, cfg.stale_factor, now=now)
        return CandleResult(rows=rows, source=src, stale=stale, degraded=degraded)
    except Exception as e:  # noqa: BLE001
        if local_rows is not None:
            return CandleResult(
                rows=local_rows, source="local", stale=True, degraded=degraded, error=str(e)
            )
        return CandleResult(rows=[], source="exchange", stale=True, degraded=degraded, error=str(e))
