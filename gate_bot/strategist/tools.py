"""On-demand market tools for the LLM strategist.

The model does not receive every series up front. It may request data with:

    {"tool_calls":[{"tool":"klines","args":{"symbol":"ETH_USDT","tf":"4h","limit":50}}]}

then continue. Final answer must be Plan JSON (no tool_calls).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..gate_client import GateClient, GateApiError
from .indicators import attach_indicators, latest_indicators
from .market import MarketConfig, resolve_candles
from .snapshot import collect_snapshot

# Human-readable tool contract (injected into system prompt when tools enabled)
TOOL_GUIDE = """【行情工具 · 按需调用】
不要假设你已经看到全部数据。需要更多 K 线/指标/盘口/账户时，**优先使用已提供的 tools 函数调用**（function calling）。
若环境未提供 tools，则可用 JSON 协议：{"tool_calls":[{"tool":"<name>","args":{...}}]}
最终决策只输出 Plan JSON。

可用函数：
- klines(symbol, tf, limit) → OHLCV
- indicators(symbol, tf, names, limit)
- ticker(symbol) → last/mark/funding/24h
- orderbook(symbol, limit)
- contract(symbol) → quanto/min_notional/rounds
- stats(symbol) / account()
默认快照只有主周期少量数据；更长历史、更高周期用工具拉。
"""

TOOL_NAMES = ("klines", "indicators", "ticker", "orderbook", "contract", "stats", "account")

# Official Chat Completions tools (function calling)
NATIVE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "klines",
            "description": "Fetch OHLCV candles for a symbol and timeframe.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. BTC_USDT"},
                    "tf": {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["symbol", "tf"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "indicators",
            "description": "Latest indicator values and series for symbol/tf.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "tf": {"type": "string"},
                    "names": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 5, "maximum": 200},
                },
                "required": ["symbol", "tf"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ticker",
            "description": "Last/mark/funding/24h stats for a symbol.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "orderbook",
            "description": "Top of book depth.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contract",
            "description": "Contract metadata (quanto, min notional, rounds, max leverage).",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stats",
            "description": "Open interest and related stats.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "account",
            "description": "Account balances and open positions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def run_tool(
    client: GateClient,
    name: str,
    args: Optional[dict] = None,
    *,
    env: str = "live",
    bot_root=None,
    market_cfg: Optional[MarketConfig] = None,
) -> Any:
    """Execute one market tool. Returns JSON-serializable result."""
    name = str(name or "").strip().lower()
    args = dict(args or {})
    if name not in TOOL_NAMES:
        return {"error": f"unknown tool {name!r}", "allowed": list(TOOL_NAMES)}

    try:
        if name == "klines":
            sym = str(args.get("symbol") or args.get("sym") or "")
            tf = str(args.get("tf") or args.get("interval") or "15m").lower()
            limit = max(1, min(int(args.get("limit") or 50), 200))
            res = resolve_candles(client, sym, tf, limit, market_cfg=market_cfg, env=env, bot_root=bot_root)
            rows = attach_indicators(list(res.rows), (market_cfg.indicators if market_cfg else None))
            return {
                "symbol": sym,
                "tf": tf,
                "source": res.source,
                "stale": res.stale,
                "rows": rows[-limit:],
            }

        if name == "indicators":
            sym = str(args.get("symbol") or args.get("sym") or "")
            tf = str(args.get("tf") or args.get("interval") or "15m").lower()
            limit = max(5, min(int(args.get("limit") or 30), 200))
            names = [str(x) for x in (args.get("names") or [])]
            if not names and market_cfg and market_cfg.indicators:
                names = list(market_cfg.indicators)
            if not names:
                names = ["ema20", "ema50", "rsi14", "atr14", "macd", "boll20"]
            res = resolve_candles(client, sym, tf, limit, market_cfg=market_cfg, env=env, bot_root=bot_root)
            rows = attach_indicators(list(res.rows), names)
            return {
                "symbol": sym,
                "tf": tf,
                "names": names,
                "latest": latest_indicators(rows, names) if rows else {},
                "rows": rows[-limit:],
            }

        if name == "ticker":
            sym = str(args.get("symbol") or args.get("sym") or "")
            t = client.get_ticker(sym)
            keys = (
                "last", "mark_price", "index_price", "funding_rate",
                "high_24h", "low_24h", "change_percentage", "volume_24h_quote",
                "highest_bid", "lowest_ask",
            )
            return {"symbol": sym, **{k: _f(t.get(k)) for k in keys}}

        if name == "orderbook":
            sym = str(args.get("symbol") or args.get("sym") or "")
            limit = max(1, min(int(args.get("limit") or 10), 50))
            ob = client.get_orderbook_top(sym, limit=limit)
            return {
                "symbol": sym,
                "bids": [{"p": _f(b.get("p")), "s": _f(b.get("s"))} for b in (ob.get("bids") or [])],
                "asks": [{"p": _f(a.get("p")), "s": _f(a.get("s"))} for a in (ob.get("asks") or [])],
            }

        if name == "contract":
            sym = str(args.get("symbol") or args.get("sym") or "")
            cm = client.get_contract(sym)
            return {
                "symbol": sym,
                "quanto_multiplier": cm.quanto_multiplier,
                "order_size_round": cm.order_size_round,
                "order_price_round": cm.order_price_round,
                "leverage_max": cm.leverage_max,
            }

        if name == "stats":
            sym = str(args.get("symbol") or args.get("sym") or "")
            rows = client.get_contract_stats(sym, limit=1) or []
            return rows[-1] if rows else {"symbol": sym, "note": "no stats"}

        if name == "account":
            acc = client.get_account() or {}
            pos = [
                {
                    "contract": p.get("contract"),
                    "size": p.get("size"),
                    "mode": p.get("mode"),
                    "entry_price": p.get("entry_price"),
                    "leverage": p.get("leverage"),
                }
                for p in (client.get_positions() or [])
                if int(p.get("size") or 0) != 0
            ]
            return {
                "available": acc.get("available"),
                "total": acc.get("total"),
                "position_mode": acc.get("position_mode"),
                "positions": pos,
            }
    except GateApiError as e:
        return {"error": str(e)[:160], "tool": name}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:160], "tool": name}

    return {"error": "unhandled"}


def extract_tool_calls(text: str) -> list[dict]:
    """Parse {"tool_calls":[...]} from model text (tolerates fences / prose)."""
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        data = json.loads(s)
        calls = data.get("tool_calls") or data.get("tools") or []
        return [c for c in calls if isinstance(c, dict)]
    except Exception:  # noqa: BLE001
        pass
    # best-effort scan
    try:
        i = s.find("{")
        j = s.rfind("}")
        if i >= 0 and j > i:
            data = json.loads(s[i : j + 1])
            calls = data.get("tool_calls") or []
            return [c for c in calls if isinstance(c, dict)]
    except Exception:  # noqa: BLE001
        return []
    return []
