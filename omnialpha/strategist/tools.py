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


- smc_map(symbol, tf, limit) → SMC 市场地图：双周期趋势(swing/internal)、估值区(Premium/Discount)、关键位(EQH/EQL)、OB/FVG 区域


- smc_events(symbol, tf, limit) → SMC 结构事件：枢轴 BOS/CHoCH 事件流、流动性扫荡(x)、OB+Breaker+活动、FVG+突袭


- sqzmom(symbol, tf, limit) → Squeeze Momentum（BB/KC 挤压 + linreg 动量）


- tv_linreg_trendlines(symbol, tf, limit, length) → TV Linreg & Trendlines：3 层回归通道(±1σ/±2σ/±3σ) + 枢轴趋势线


- tv_rsi_yata(symbol, tf, limit, length) → TV RSI Yata：平滑 RSI + MA + BB + RSI 蜡烛 + OB/OS + 直方图 + RSI-MACD + HH/HL/LH/LL 结构


- tv_lr_ha_candles(symbol, tf, limit, length) → TV LR HA Candles：线性回归 Heikin-Ashi + T3 + ATR 波动带


默认快照只有主周期少量数据；更长历史、更高周期用工具拉。


"""





TOOL_NAMES = (


    "klines", "indicators", "ticker", "orderbook", "contract", "stats", "account",


    # aux-cache (pa-data-source) — existing data only


    "trades_flow", "liquidations", "market_stats", "tech_analysis", "coin_info", "onchain", "social",


    "overview", "sentiment", "macro",


    "smc_map", "smc_events", "sqzmom", "skill", "skill_ref",

    # TV Pine 指标（真实指标名）
    "tv_linreg_trendlines", "tv_rsi_yata", "tv_lr_ha_candles",


)








def _aux_db(bot_root=None):


    """Return path to pa-data-source aux_cache.db or None."""


    import os


    from pathlib import Path





    env = os.environ.get("OMNIALPHA_AUX") or os.environ.get("GATE_AUX_DB")


    if env:


        p = Path(env).expanduser()


        return p if p.is_file() else None


    bases = []


    if bot_root:


        bases.append(Path(bot_root) / "pa-data-source" / "aux-data" / "aux_cache.db")


    bases.append(Path.cwd() / "pa-data-source" / "aux-data" / "aux_cache.db")


    for p in bases:


        if p.is_file():


            return p


    return None








def _aux_query(bot_root, sql: str, params: tuple = (), limit_cols=None) -> list:


    import sqlite3





    db = _aux_db(bot_root)


    if not db:


        return [{"error": "aux_cache.db not found (pa-data-source not running?)"}]


    try:


        conn = sqlite3.connect(str(db), timeout=5)


        conn.row_factory = sqlite3.Row


        try:


            rows = conn.execute(sql, params).fetchall()


            out = [dict(r) for r in rows]


            return out


        finally:


            conn.close()


    except Exception as e:  # noqa: BLE001


        return [{"error": f"aux query failed: {e}"[:160]}]





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


            "description": "Latest indicator values and series for symbol/tf. names 必须带周期且全小写，例：ema20 / rsi14 / atr14 / boll20。",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string"},


                    "tf": {"type": "string"},


                    "names": {"type": "array", "items": {"type": "string"}, "description": "指标名列表。必须带周期且全小写，例：ema20 / rsi14 / atr14 / boll20。合法：emaN rsiN atrN maN smaN rmaN wmaN vwmaN、macd、bollN|bollN_K|boll、stochN cciN wrN mfiN adxN、vwapN obv supertrend。会被拒的写法：rsi / atr / bb20 / bbands / bb / EMA20（大写）。"},


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


            "description": "REST account: balances, positions, open orders, TP/SL protections (authoritative, not DB).",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string", "description": "optional; default uses positions/open orders symbols"}


                },


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "trades_flow",


            "description": "Recent public trades from local aux cache (tape).",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string"},


                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},


                },


                "required": ["symbol"],


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "liquidations",


            "description": "Recent liquidations from local aux cache.",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string"},


                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},


                },


                "required": ["symbol"],


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "market_stats",


            "description": "Open interest, long/short ratios, liq sizes from aux cache.",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string"},


                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},


                },


                "required": ["symbol"],


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "tech_analysis",


            "description": "Aggregated technical analysis summary from aux cache.",


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


            "name": "coin_info",


            "description": "Coin metadata and sentiment score from aux cache.",


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


            "name": "onchain",


            "description": "On-chain activity snapshot from aux cache.",


            "parameters": {


                "type": "object",


                "properties": {"token": {"type": "string"}},


                "required": ["token"],


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "social",


            "description": "Recent social posts + sentiment from aux cache.",


            "parameters": {


                "type": "object",


                "properties": {


                    "coin": {"type": "string"},


                    "limit": {"type": "integer", "minimum": 1, "maximum": 30},


                },


                "required": ["coin"],


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "overview",


            "description": "Market overview: fear/greed, BTC/ETH dominance, total mcap, AHR999.",


            "parameters": {


                "type": "object",


                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "sentiment",


            "description": "Aggregate social sentiment per coin (mentions, long/short ratios).",


            "parameters": {


                "type": "object",


                "properties": {


                    "coin": {"type": "string"},


                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},


                },


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "macro",


            "description": "Macro series (CPI, rates, GDP, NFP) and calendar events.",


            "parameters": {


                "type": "object",


                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "smc_map",


            "description": "SMC market map (where am I): dual-timeframe trend (swing/internal), premium/discount zones, key liquidity levels (EQH/EQL), OB/FVG regions. Use for direction and location.",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string", "description": "e.g. BTC_USDT"},


                    "tf": {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]},


                    "limit": {"type": "integer", "minimum": 30, "maximum": 300},


                },


                "required": ["symbol"],


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "smc_events",


            "description": "SMC structure events (what just fired): pivot BOS/CHoCH event stream, liquidity sweeps (x), order blocks with breakers/activity, FVG with breakers/raids. Use for timing and triggers.",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string", "description": "e.g. BTC_USDT"},


                    "tf": {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]},


                    "limit": {"type": "integer", "minimum": 30, "maximum": 300},


                },


                "required": ["symbol"],


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "sqzmom",


            "description": "Squeeze Momentum (LazyBear): BB vs KC squeeze state + linreg momentum histogram. sqz_state: 1=squeeze on, -1=squeeze off, 0=no squeeze.",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string", "description": "e.g. BTC_USDT"},


                    "tf": {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]},


                    "limit": {"type": "integer", "minimum": 50, "maximum": 300},


                    "length": {"type": "integer", "minimum": 5, "maximum": 50, "description": "BB/KC length (default 20)"},


                },


                "required": ["symbol"],


            },


        },


    },


]


# SkillKit: L2 on-demand skill loading (read-only advisory injection)
from ..skillkit.tool import SKILL_TOOL_DEF as _SKILL_TOOL_DEF  # noqa: E402
from ..skillkit.tool import SKILL_REF_TOOL_DEF as _SKILL_REF_TOOL_DEF  # noqa: E402

NATIVE_TOOLS.append(_SKILL_TOOL_DEF)
NATIVE_TOOLS.append(_SKILL_REF_TOOL_DEF)

# TV Pine 指标工具（Linreg & Trendlines / RSI Yata / LR HA Candles）
from .tv_tools import TV_TOOL_DEFS as _TV_TOOL_DEFS  # noqa: E402

NATIVE_TOOLS.extend(_TV_TOOL_DEFS)








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


    bot_id: str = "",


    skill_ids: Optional[list] = None,


) -> Any:


    """Execute one market tool. Returns JSON-serializable result."""


    name = str(name or "").strip().lower()


    args = dict(args or {})


    if name not in TOOL_NAMES:


        return {"error": f"unknown tool {name!r}", "allowed": list(TOOL_NAMES)}


    # skill: SkillKit L2 loading (no gate client needed)
    if name == "skill":
        from pathlib import Path as _P
        from ..skillkit.registry import SkillRegistry
        from ..skillkit.tool import run_skill_tool
        from ..skillkit.models import SkillError as _SkillError

        root = _P(bot_root) if bot_root else _P.cwd()
        reg = SkillRegistry()
        reg.scan([root / ".mimocode" / "skills", root / "skills"])
        # 白名单由 runner 显式注入（不信任 LLM 传参）：
        #   None = 默认全可见；[] = 该 bot 无 skill；[..] = 白名单
        enabled = skill_ids if skill_ids is not None else args.get("enabled_skills")
        try:
            text = run_skill_tool(
                reg,
                {"name": args.get("name")},
                bot_id=str(bot_id or args.get("bot_id") or ""),
                enabled_ids=list(enabled) if isinstance(enabled, (list, tuple)) else None,
                root=root,
            )
            return {"skill": args.get("name"), "content": text}
        except _SkillError as e:
            return {"error": str(e), "available": reg.ids()}

    # skill_ref: SkillKit L3 — read bundled reference/script/asset (contained)
    if name == "skill_ref":
        from pathlib import Path as _P
        from ..skillkit.registry import SkillRegistry
        from ..skillkit.tool import run_skill_ref
        from ..skillkit.models import SkillError as _SkillError

        root = _P(bot_root) if bot_root else _P.cwd()
        reg = SkillRegistry()
        reg.scan([root / ".mimocode" / "skills", root / "skills"])
        enabled = skill_ids if skill_ids is not None else args.get("enabled_skills")
        try:
            text = run_skill_ref(
                reg,
                {"name": args.get("name"), "path": args.get("path")},
                bot_id=str(bot_id or args.get("bot_id") or ""),
                enabled_ids=list(enabled) if isinstance(enabled, (list, tuple)) else None,
                root=root,
            )
            return {"skill": args.get("name"), "path": args.get("path"), "content": text}
        except _SkillError as e:
            return {"error": str(e), "available": reg.ids()}





    try:


        if name.startswith("tv_"):
            from .tv_tools import run_tv_tool
            return run_tv_tool(client, name, args, env=env, bot_root=bot_root,
                               market_cfg=market_cfg)


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


            # Always REST (authoritative); local account.db may lag (no keys / stalled push)


            acc = client.get_account() or {}


            pos = []


            for p in (client.get_positions() or []):


                if int(p.get("size") or 0) == 0:


                    continue


                pos.append({


                    "contract": p.get("contract"),


                    "size": p.get("size"),


                    "mode": p.get("mode"),


                    "entry_price": p.get("entry_price"),


                    "leverage": p.get("leverage"),


                    "unrealised_pnl": p.get("unrealised_pnl"),


                    "liq_price": p.get("liq_price"),


                    "margin": p.get("margin"),


                })


            open_orders = []


            price_orders = []


            syms = args.get("symbols") or ([args.get("symbol")] if args.get("symbol") else [])


            if not syms:


                try:


                    rows = client.list_orders() or []


                    syms = sorted({o.get("contract") for o in rows if o.get("contract")})


                except Exception:  # noqa: BLE001


                    syms = []


                if not syms:


                    # price-only leftovers (TP/SL without entry)


                    try:


                        ps = client.list_price_orders(None) or []


                        syms = sorted({


                            (p.get("contract") or (p.get("initial") or {}).get("contract") or "")


                            for p in ps


                        } - {""})


                    except Exception:  # noqa: BLE001


                        pass


            for s in set(x for x in syms if x) or ({args["symbol"]} if args.get("symbol") else set()):


                try:


                    for o in (client.list_orders(s) or []):


                        open_orders.append({


                            "id": o.get("id"),


                            "contract": s,


                            "size": o.get("size"),


                            "price": o.get("price"),


                            "left": o.get("left"),


                            "status": o.get("status"),


                            "text": o.get("text"),


                            "tif": o.get("tif"),


                        })


                except Exception as e:  # noqa: BLE001


                    open_orders.append({"error": str(e)[:80], "contract": s})


                try:


                    for p in (client.list_price_orders(s) or []):


                        ini = p.get("initial") or {}


                        trg = p.get("trigger") or {}


                        price_orders.append({


                            "id": p.get("id"),


                            "contract": s,


                            "size": ini.get("size"),


                            "trigger_price": trg.get("price"),


                            "rule": trg.get("rule"),


                            "text": ini.get("text") or p.get("text"),


                            "status": p.get("status"),


                        })


                except Exception as e:  # noqa: BLE001


                    price_orders.append({"error": str(e)[:80], "contract": s})


            return {


                "source": "rest",


                "available": acc.get("available"),


                "total": acc.get("total"),


                "position_mode": acc.get("position_mode"),


                "positions": pos,


                "open_orders": open_orders,


                "protections": price_orders,


            }





        # ── aux_cache.db (existing pa-data-source data) ──


        if name == "trades_flow":


            sym = str(args.get("symbol") or args.get("contract") or args.get("sym") or "")


            lim = max(1, min(int(args.get("limit") or 50), 200))


            rows = _aux_query(


                bot_root,


                "SELECT time, contract, price, size FROM trades WHERE contract=? ORDER BY time DESC LIMIT ?",


                (sym, lim),


            )


            return {"symbol": sym, "trades": rows}





        if name == "liquidations":


            sym = str(args.get("symbol") or args.get("contract") or args.get("sym") or "")


            lim = max(1, min(int(args.get("limit") or 50), 200))


            rows = _aux_query(


                bot_root,


                "SELECT time, contract, size, order_size, order_price, fill_price FROM liquidations "


                "WHERE contract=? ORDER BY time DESC LIMIT ?",


                (sym, lim),


            )


            return {"symbol": sym, "liquidations": rows}





        if name == "market_stats":


            sym = str(args.get("symbol") or args.get("contract") or args.get("sym") or "")


            rows = _aux_query(


                bot_root,


                "SELECT fetched_ts, contract, lsr_taker, lsr_account, long_liq_size, short_liq_size, "


                "open_interest, open_interest_usd, top_lsr_account, mark_price "


                "FROM market_stats_ts WHERE contract=? ORDER BY fetched_ts DESC LIMIT ?",


                (sym, max(1, min(int(args.get("limit") or 5), 20))),


            )


            return {"symbol": sym, "market_stats": rows}





        if name == "tech_analysis":


            sym = str(args.get("symbol") or args.get("sym") or "").replace("_", "").upper() or ""


            # aux may store BTC or BTCUSDT


            keys = [sym, sym.replace("USDT", "")] if sym else []


            rows = []


            for k in keys:


                rows = _aux_query(


                    bot_root,


                    "SELECT fetched_ts, symbol, period, signal, timeframes_json FROM tech_analysis_ts "


                    "WHERE symbol=? ORDER BY fetched_ts DESC LIMIT 3",


                    (k,),


                )


                if rows and not (rows[0] or {}).get("error"):


                    break


            return {"symbol": sym, "tech_analysis": rows}





        if name == "coin_info":


            sym = str(args.get("symbol") or args.get("sym") or "").replace("_", "").upper()


            rows = _aux_query(


                bot_root,


                "SELECT fetched_ts, symbol, name, chain, category, market_value, sentiment_score "


                "FROM coin_info_ts WHERE symbol=? ORDER BY fetched_ts DESC LIMIT 2",


                (sym.replace("USDT", ""),),


            )


            return {"symbol": sym, "coin_info": rows}





        if name == "onchain":


            token = str(args.get("token") or args.get("symbol") or "").replace("_", "").upper()


            rows = _aux_query(


                bot_root,


                "SELECT fetched_ts, token, chain, daily_active_addresses, daily_transfer_volume, "


                "new_address_count_7d, holder_count, data_quality FROM onchain_ts "


                "WHERE token=? ORDER BY fetched_ts DESC LIMIT 2",


                (token.replace("USDT", ""),),


            )


            return {"token": token, "onchain": rows}





        if name == "social":


            coin = str(args.get("coin") or args.get("symbol") or "").replace("_", "").upper()


            lim = max(1, min(int(args.get("limit") or 10), 30))


            rows = _aux_query(


                bot_root,


                "SELECT fetched_ts, coin, author, content, upvotes, sentiment_label, sentiment_score "


                "FROM social_posts_ts WHERE coin=? ORDER BY fetched_ts DESC LIMIT ?",


                (coin.replace("USDT", ""), lim),


            )


            return {"coin": coin, "social": rows}





        if name == "overview":


            lim = max(1, min(int(args.get("limit") or 3), 20))


            rows = _aux_query(


                bot_root,


                "SELECT fetched_ts, btc_price, btc_dominance, eth_dominance, fear_greed, fear_greed_label, "


                "total_market_cap, total_volume_24h, market_cap_change_24h, altcoin_season_index, ahr999 "


                "FROM overview_ts ORDER BY fetched_ts DESC LIMIT ?",


                (lim,),


            )


            return {"overview": rows}





        if name == "sentiment":


            coin = str(args.get("coin") or args.get("symbol") or "").replace("_", "").upper()


            lim = max(1, min(int(args.get("limit") or 5), 20))


            q = ("SELECT fetched_ts, coin, mention_count, overall_sentiment, sentiment_label, "


                 "positive_ratio, neutral_ratio, negative_ratio FROM sentiment_ts")


            if coin:


                rows = _aux_query(bot_root, q + " WHERE coin=? ORDER BY fetched_ts DESC LIMIT ?",


                                  (coin.replace("USDT", ""), lim))


            else:


                rows = _aux_query(bot_root, q + " ORDER BY fetched_ts DESC LIMIT ?", (lim,))


            return {"sentiment": rows}





        if name == "macro":


            lim = max(1, min(int(args.get("limit") or 5), 20))


            series = _aux_query(


                bot_root,


                "SELECT fetched_ts, cpi_yoy, fed_funds_rate, gdp_growth, nonfarm_payroll, pce_yoy, "


                "unemployment_rate FROM macro_ts ORDER BY fetched_ts DESC LIMIT ?",


                (lim,),


            )


            events = _aux_query(


                bot_root,


                "SELECT event_id, event_date, event_name, event_type, event_time, status "


                "FROM macro_events ORDER BY event_date DESC LIMIT ?",


                (lim,),


            )


            return {"macro_series": series, "macro_events": events}





        if name == "smc_map":


            from .smc_map import compute_smc_map, smc_map_summary





            sym = str(args.get("symbol") or args.get("sym") or "BTC_USDT")


            tf = str(args.get("tf") or args.get("interval") or "15m").lower()


            lim = max(30, min(int(args.get("limit") or 150), 300))


            # same hybrid path as klines: per-exchange local DB → that venue's REST


            res = resolve_candles(client, sym, tf, lim, market_cfg=market_cfg, env=env, bot_root=bot_root)


            rows = list(res.rows or [])


            out = smc_map_summary(compute_smc_map(rows))


            out.update({


                "symbol": sym, "tf": tf, "n": len(rows),


                "source": res.source, "stale": res.stale, "degraded": res.degraded,


                "set": "map",


            })


            return out





        if name == "smc_events":


            from .smc_events import compute_smc_events, smc_events_summary





            sym = str(args.get("symbol") or args.get("sym") or "BTC_USDT")


            tf = str(args.get("tf") or args.get("interval") or "15m").lower()


            lim = max(30, min(int(args.get("limit") or 150), 300))


            res = resolve_candles(client, sym, tf, lim, market_cfg=market_cfg, env=env, bot_root=bot_root)


            rows = list(res.rows or [])


            out = smc_events_summary(compute_smc_events(rows))


            out.update({


                "symbol": sym, "tf": tf, "n": len(rows),


                "source": res.source, "stale": res.stale, "degraded": res.degraded,


                "set": "events",


            })


            return out





        if name == "sqzmom":


            





            sym = str(args.get("symbol") or args.get("sym") or "BTC_USDT")


            tf = str(args.get("tf") or args.get("interval") or "15m").lower()


            lim = max(50, min(int(args.get("limit") or 200), 300))


            bb_len = int(args.get("bb_length") or args.get("length") or 20)


            kc_len = int(args.get("kc_length") or args.get("length") or 20)


            res = resolve_candles(client, sym, tf, lim, market_cfg=market_cfg, env=env, bot_root=bot_root)


            rows = list(res.rows or [])


            wanted = [


                f"sqzmom{bb_len}_{kc_len}",


                f"sqzmom{bb_len}_{kc_len}_state",


                f"sqzmom{bb_len}_{kc_len}_up",


                f"sqzmom{bb_len}_{kc_len}_on",


            ]


            attach_indicators(rows, wanted)


            last = latest_indicators(rows, wanted)


            out = {


                "symbol": sym, "tf": tf, "n": len(rows),


                "source": res.source, "stale": res.stale, "degraded": res.degraded,


                "bb_length": bb_len, "kc_length": kc_len,


                "sqz_mom": last.get(wanted[0]),


                "sqz_state": last.get(wanted[1]),   # 1=sqzOn  -1=sqzOff  0=noSqz


                "momentum_up": last.get(wanted[2]),


                "sqz_on": last.get(wanted[3]),


            }


            return out


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


