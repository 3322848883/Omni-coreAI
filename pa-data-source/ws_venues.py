"""多所公开 WebSocket K 线源（对齐 Gate kline_watcher 的 WS 实时模式）。

每个 venue 实现：
  - url: WS 地址
  - subscribe(symbol, interval) -> list[dict]  订阅报文
  - parse(msg) -> list[dict]                    解出 [{t,o,h,l,c,v,sum}]

能力：真推送 → 写本地库；连不上/断线 → 由上层降级 REST 并补全缺口。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

# 内部 interval -> 各所 WS 订阅用周期标识
TF = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _sym_strip(symbol: str) -> str:
    return str(symbol).replace("_", "").upper()  # BTC_USDT -> BTCUSDT


def _sym_okx(symbol: str) -> str:
    s = str(symbol).upper()
    return s.replace("_", "-") + "-SWAP" if "_" in s else s  # BTC_USDT -> BTC-USDT-SWAP


class VenueWS:
    name = ""
    url = ""
    # 是否确认本网络可达（探测后可写回，供调度参考）
    reachable: Optional[bool] = None

    def subscribe(self, symbol: str, interval: str) -> list[dict]:
        raise NotImplementedError

    def parse(self, msg: Any) -> list[dict]:
        raise NotImplementedError

    def sub_key(self, symbol: str, interval: str) -> str:
        return f"{symbol}|{interval}"


class HyperliquidWS(VenueWS):
    name = "hyperliquid"
    url = "wss://api.hyperliquid.xyz/ws"

    def subscribe(self, symbol: str, interval: str) -> list[dict]:
        return [{
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": symbol.split("_")[0].upper(), "interval": interval},
        }]

    def parse(self, msg: Any) -> list[dict]:
        try:
            d = json.loads(msg) if isinstance(msg, (str, bytes)) else msg
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(d, dict):
            return []
        if d.get("channel") != "candle":
            return []
        r = d.get("data") or {}
        t = r.get("t")
        if t is None:
            return []
        coin = str(r.get("s") or "BTC").upper()
        return [{
            "t": int(t) // 1000,
            "o": _f(r.get("o")), "h": _f(r.get("h")), "l": _f(r.get("l")), "c": _f(r.get("c")),
            "v": _f(r.get("v")) or 0.0, "sum": 0.0,
            "symbol": f"{coin}_USDT", "interval": str(r.get("i") or ""),
        }]


class BinanceWS(VenueWS):
    name = "binance"
    # combined stream
    url = "wss://fstream.binance.com/stream"

    def subscribe(self, symbol: str, interval: str) -> list[dict]:
        s = _sym_strip(symbol).lower()
        return [{"method": "SUBSCRIBE", "params": [f"{s}@kline_{interval}"], "id": 1}]

    def parse(self, msg: Any) -> list[dict]:
        try:
            d = json.loads(msg) if isinstance(msg, (str, bytes)) else msg
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(d, dict):
            return []
        payload = d.get("data") if isinstance(d.get("data"), dict) else d
        k = payload.get("k") if isinstance(payload, dict) else None
        if not isinstance(k, dict):
            return []
        t = k.get("t")
        if t is None:
            return []
        # symbol 在事件体 data.s（或 k.s）—— Binance 两处都可能给
        sym = str((payload or {}).get("s") or k.get("s") or "")
        internal = sym
        if sym and "_" not in sym:
            # BTCUSDT -> BTC_USDT
            if sym.endswith("USDT"):
                internal = f"{sym[:-4]}_USDT"
        return [{
            "t": int(t) // 1000,
            "o": _f(k.get("o")), "h": _f(k.get("h")), "l": _f(k.get("l")), "c": _f(k.get("c")),
            "v": _f(k.get("v")) or 0.0,
            "sum": _f(k.get("q")) or 0.0,
            "symbol": internal, "interval": str(k.get("i") or ""),
        }]


class OkxWS(VenueWS):
    name = "okx"
    url = "wss://ws.okx.com:8443/ws/v5/public"

    def subscribe(self, symbol: str, interval: str) -> list[dict]:
        # OKX candle channel: candle1m / candle5m / candle15m / candle1H / candle4H / candle1D
        bar = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1H", "4h": "4H", "1d": "1D",
        }.get(interval, "15m")
        return [{"op": "subscribe", "args": [{"channel": f"candle{bar}", "instId": _sym_okx(symbol)}]}]

    def parse(self, msg: Any) -> list[dict]:
        try:
            d = json.loads(msg) if isinstance(msg, (str, bytes)) else msg
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(d, dict):
            return []
        if not str(d.get("arg", {}).get("channel", "")).startswith("candle"):
            return []
        out = []
        for r in d.get("data") or []:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            ts = int(r[0]) // 1000
            inst = str(d.get("arg", {}).get("instId") or "")
            out.append({
                "t": ts,
                "o": _f(r[1]), "h": _f(r[2]), "l": _f(r[3]), "c": _f(r[4]),
                "v": _f(r[5]) or 0.0,
                "sum": _f(r[7]) if len(r) > 7 else 0.0,
                "symbol": inst.replace("-SWAP", "").replace("-", "_"),
                "interval": str(d.get("arg", {}).get("channel", "")).replace("candle", "").replace("H", "h").replace("D", "d"),
            })
        return out


class BybitWS(VenueWS):
    name = "bybit"
    url = "wss://stream.bybit.com/v5/public/linear"

    def subscribe(self, symbol: str, interval: str) -> list[dict]:
        iv = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}.get(interval, "15")
        return [{"op": "subscribe", "args": [f"kline.{iv}.{_sym_strip(symbol)}"]}]

    def parse(self, msg: Any) -> list[dict]:
        try:
            d = json.loads(msg) if isinstance(msg, (str, bytes)) else msg
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(d, dict):
            return []
        topic = str(d.get("topic") or "")
        if not topic.startswith("kline."):
            return []
        out = []
        for r in d.get("data") or []:
            t = r.get("start")
            if t is None:
                continue
            sym = str(r.get("symbol") or "")
            internal = f"{sym[:-4]}_USDT" if sym.endswith("USDT") and "_" not in sym else sym
            out.append({
                "t": int(t) // 1000,
                "o": _f(r.get("open")), "h": _f(r.get("high")), "l": _f(r.get("low")), "c": _f(r.get("close")),
                "v": _f(r.get("volume")) or 0.0,
                "sum": _f(r.get("turnover")) or 0.0,
                "symbol": internal, "interval": "",
            })
        return out


class BitgetWS(VenueWS):
    name = "bitget"
    url = "wss://ws.bitget.com/v2/ws/public"

    def subscribe(self, symbol: str, interval: str) -> list[dict]:
        bar = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1H", "4h": "4H", "1d": "1D",
        }.get(interval, "15m")
        return [{
            "op": "subscribe",
            "arg": {"instType": "USDT-FUTURES", "channel": f"candle{bar}", "instId": _sym_strip(symbol)},
        }]

    def parse(self, msg: Any) -> list[dict]:
        try:
            d = json.loads(msg) if isinstance(msg, (str, bytes)) else msg
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(d, dict):
            return []
        arg = d.get("arg") or {}
        if not str(arg.get("channel", "")).startswith("candle"):
            return []
        out = []
        for r in d.get("data") or []:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            # [ts, o, h, l, c, vol, ...]
            ts = int(r[0]) // 1000 if int(r[0]) > 1e12 else int(r[0])
            inst = str(arg.get("instId") or "")
            internal = f"{inst[:-4]}_USDT" if inst.endswith("USDT") and "_" not in inst else inst
            out.append({
                "t": ts,
                "o": _f(r[1]), "h": _f(r[2]), "l": _f(r[3]), "c": _f(r[4]),
                "v": _f(r[5]) or 0.0, "sum": 0.0,
                "symbol": internal, "interval": "",
            })
        return out


VENUES: dict[str, VenueWS] = {
    "hyperliquid": HyperliquidWS(),
    "binance": BinanceWS(),
    "okx": OkxWS(),
    "bybit": BybitWS(),
    "bitget": BitgetWS(),
}


def get_venue(name: str) -> VenueWS:
    key = str(name).strip().lower()
    if key not in VENUES:
        raise ValueError(f"no WS venue {name!r}; known: {sorted(VENUES)}")
    return VENUES[key]


class WSConsumer:
    """连一个 venue 的 WS，订阅并回调 candles；失败抛异常由上层降级 REST。"""

    def __init__(self, venue: VenueWS, subscriptions: list[tuple[str, str]],
                 on_candles: Callable[[str, str, list[dict]], None],
                 on_status: Optional[Callable[[str], None]] = None):
        self.venue = venue
        self.subscriptions = subscriptions
        self.on_candles = on_candles
        self.on_status = on_status or (lambda s: None)
        self._ws = None
        self._want: dict[str, tuple[str, str]] = {}  # coarse key -> (symbol, interval)

    def run_forever(self, ping_interval: int = 20) -> None:
        import ssl
        import websocket

        v = self.venue
        for sym, iv in self.subscriptions:
            self._want[f"{sym}|{iv}"] = (sym, iv)
            # 同时登记 symbol 粗匹配（各所回报可能不带 interval）
            self._want.setdefault(f"{sym}|", (sym, iv))

        self.on_status(f"{v.name}: connecting {v.url}")
        ws = websocket.WebSocketApp(
            v.url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        ws.run_forever(
            sslopt={"cert_reqs": ssl.CERT_NONE},
            ping_interval=ping_interval,
            ping_timeout=8,
        )

    def _on_open(self, ws) -> None:
        v = self.venue
        self.on_status(f"{v.name}: connected")
        for sym, iv in self.subscriptions:
            for m in v.subscribe(sym, iv):
                ws.send(json.dumps(m))
            self.on_status(f"{v.name}: sub {sym} {iv}")

    def _on_message(self, ws, message) -> None:
        rows = self.venue.parse(message)
        if not rows:
            return
        by_key: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            sym = str(r.get("symbol") or "")
            iv = str(r.get("interval") or "")
            key = None
            if f"{sym}|{iv}" in self._want:
                key = (sym, iv)
            elif f"{sym}|" in self._want:
                key = self._want[f"{sym}|"]
            if key is None:
                continue
            by_key.setdefault(key, []).append(r)
        for (sym, iv), rs in by_key.items():
            self.on_candles(sym, iv, rs)

    def _on_error(self, ws, error) -> None:
        self.on_status(f"{self.venue.name}: error {type(error).__name__}: {error}")

    def _on_close(self, ws, code, msg) -> None:
        self.on_status(f"{self.venue.name}: closed {code} {msg}")
