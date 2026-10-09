"""On-demand market tools for the LLM strategist.





The model does not receive every series up front. It may request data with:





    {"tool_calls":[{"tool":"klines","args":{"symbol":"ETH_USDT","tf":"4h","limit":50}}]}





then continue. Final answer must be Plan JSON (no tool_calls).


"""


from __future__ import annotations





import json


from typing import Any, Optional





from ..gate_client import GateClient, GateApiError


from .indicators import _resolve_wanted, attach_indicators, latest_indicators


from .market import MarketConfig, resolve_candles


from .snapshot import collect_snapshot
from .symbols import resolve_symbol_arg, symbol_error_payload





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


- tv_delta_flow_profile(symbol, tf, limit, lookback, rows, polarity) → TV Delta Flow Profile：逐价位 money flow（量×价位）+ delta（买−卖）+ POC 与 PoC 迁移轨迹


- tv_oi_visible_range(symbol, tf, limit, rows, va_pct, delta_rows) → TV OI Visible Range：持仓量四象限（价涨跌 × OI 增减）+ 各象限 POC/VA + 档位汇总


- tv_vol_oi_footprint(symbol, tf, limit, resolution, mode) → TV Volume/OI Footprint：逐价位成交量足迹（K线几何分摊，影线半绿半红）；mode=oi 改用 ΔOI


- tv_cdv(symbol, tf, limit, ha, sma1, sma2, ema1, ema2) → TV Cumulative Delta Volume：逐根 delta 的累积（delta 由 K 线几何估算），可选 HA 与均线


- taker_delta(symbol, tf, limit, tail) → 真 Delta/CVD（交易所实测 taker 买卖量，非估算）+ 大户持仓 + 资金费率 + 爆仓量


- orderflow_tape(symbol, limit) → 逐秒主动买卖量/Delta/CVD/大单（WS 逐笔实采，size 符号即方向）


- orderflow_footprint(symbol, minutes, rows) → 逐价位真实买卖量 + POC + 失衡档位（判据：单档买卖比 ≥3:1）


- orderbook_state(symbol, limit) → 盘口状态四维度（价差/深度/撤单率/成交密度）+ 分级（微观信号的前置门）


- orderbook_walls(symbol, min_age, limit) → 长寿挂单（age ≥ 30s）及其结局（被吃/被撤）


默认快照只有主周期少量数据；更长历史、更高周期用工具拉。


"""





TOOL_NAMES = (


    "klines", "indicators", "ticker", "orderbook", "contract", "stats", "account",
    "taker_delta",


    # aux-cache (pa-data-source) — existing data only


    "trades_flow", "liquidations", "market_stats", "tech_analysis", "coin_info", "onchain", "social",


    "overview", "sentiment", "macro",


    "smc_map", "smc_events", "sqzmom", "skill", "skill_ref",

    # 记忆：按 cycle_id 取回某轮决策全文（`[近期决策索引]` 的配套检索入口）
    "journal_lookup",

    # TV Pine 指标（真实指标名）
    "tv_linreg_trendlines", "tv_rsi_yata", "tv_lr_ha_candles",
    "tv_delta_flow_profile", "tv_oi_visible_range", "tv_vol_oi_footprint",
    "tv_cdv", "tv_wyckoff",

    # 订单流实采（pa-data-source/orderflow.db，WS 采）
    "orderflow_tape", "orderflow_footprint", "orderbook_state", "orderbook_walls",


)


# kline 表自带 ema20/atr14 两列，bar dict 会一路带进工具返回 —— 即使
# `strategist.market.indicators: []` 也关不掉。返回前按 wanted 剥掉。
_CARRIED_INDICATOR_KEYS = ("ema20", "atr14")


def _strip_carried_indicators(row: dict, wanted) -> dict:
    allow = set(wanted or ())
    return {k: v for k, v in row.items()
            if k not in _CARRIED_INDICATOR_KEYS or k in allow}








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


                    "symbol": {"type": "string", "description": "币种（取【品种宇宙】里的值）"},


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


            "description": (
                "Top-of-book depth (bid/ask sizes). Used to judge absorption: whether a "
                "level is being defended or eaten."
            ),


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


            "description": (
                "Raw `/contract_stats` records (25 fields, including the taker buy/sell "
                "sizes). Use it when you need a field the summarised tools do not expose."
            ),


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


            "description": (
                "Recent liquidations from local aux cache. `size` is SIGNED: "
                "negative = a long was liquidated, positive = a short was liquidated. "
                "Read it as a forced-flow event stream, not as a direction signal."
            ),


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


            "description": (
                "Open interest, long/short ratios and liquidation sizes from the aux cache "
                "(1h granularity). Snapshot only — no per-level detail."
            ),


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


            "description": "SMC market map (where am I): dual-timeframe trend (swing/internal), premium/discount zones, key liquidity levels (EQH/EQL), OB/FVG regions. Use for direction and location. NOTE: premium_discount here is computed from the LAST SWING RANGE (trailing top/bottom); smc_events computes the same-named field from FULL HISTORY — do NOT compare premium_top/equilibrium/current_zone across the two tools, read each one's own `source` field. bar/bar_index is relative to this call's window and NOT comparable across tools or timeframes; use `time` (unix sec) to align. FIELD NOTES: order_blocks[].bl_pos/br_pos = buy/sell activity bar COUNTS inside the OB (not price levels); [].vol_share = this OB's share among OBs of the SAME direction (sums to 1 per direction), not absolute volume.",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string", "description": "币种（取【品种宇宙】里的值）"},


                    "tf": {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]},


                    "limit": {"type": "integer", "minimum": 30, "maximum": 300, "description": "Minimum bars to analyze. The tool internally fetches at least 500 bars so ta.atr(200) stays valid and the structure state machine is fully warmed up — results are identical regardless of the limit you pass. See bars_analyzed in the result for the actual count."},


                },


                "required": ["symbol"],


            },


        },


    },


    {


        "type": "function",


        "function": {


            "name": "smc_events",


            "description": "SMC structure events (what just fired): pivot BOS/CHoCH event stream, liquidity sweeps (x), order blocks with breakers/activity, FVG with breakers/raids. Use for timing and triggers. NOTE: premium_discount here is computed from FULL HISTORY (max high / min low); smc_map computes the same-named field from the last SWING RANGE — do NOT compare them, read each one's own `source` field. bar is relative to this call's window and NOT comparable across tools or timeframes; use `time` (unix sec) to align. FIELD NOTES: order_blocks[].dir = the OB candle's own direction (1 = close>open), NOT the OB's bias; [].bl_pos/br_pos = buy/sell activity bar COUNTS (not price levels); [].vol_share = this OB's share among OBs of the SAME direction (sums to 1 per direction), not absolute volume; sweeps[].kind = which structure line got swept (choch/bos), NOT the sweep type.",


            "parameters": {


                "type": "object",


                "properties": {


                    "symbol": {"type": "string", "description": "币种（取【品种宇宙】里的值）"},


                    "tf": {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]},


                    "limit": {"type": "integer", "minimum": 30, "maximum": 300, "description": "Minimum bars to analyze. The tool internally fetches at least 500 bars so ta.atr(200) stays valid and the structure state machine is fully warmed up — results are identical regardless of the limit you pass. See bars_analyzed in the result for the actual count."},


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


                    "symbol": {"type": "string", "description": "币种（取【品种宇宙】里的值）"},


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

# 记忆检索：按 cycle_id 取回某轮决策全文。prompt 的 `[近期决策索引]` 只给
# cycle_id + decision 一行 —— 这个工具是它的兑现出口，也是「近况段体积不随
# 历史增长」的前提（否则索引只是一串无法兑现的编号）。
_JOURNAL_LOOKUP_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "journal_lookup",
        "description": (
            "Look up one past decision cycle of THIS bot by cycle_id, returning its "
            "decision / reasoning / execution result / referenced cycles. Use the "
            "cycle_ids listed in the '[近期决策索引]' block of the prompt when you "
            "need to know what an earlier round actually judged and did."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cycle_id": {
                    "type": "string",
                    "description": "e.g. btc-pa-15m-20261006-213 (from [近期决策索引])",
                },
            },
            "required": ["cycle_id"],
        },
    },
}
NATIVE_TOOLS.append(_JOURNAL_LOOKUP_TOOL_DEF)

# TV Pine 指标工具（Linreg & Trendlines / RSI Yata / LR HA Candles）
from .tv_tools import TV_TOOL_DEFS as _TV_TOOL_DEFS  # noqa: E402

NATIVE_TOOLS.extend(_TV_TOOL_DEFS)

# 订单流实采工具（读 pa-data-source/orderflow.db）—— 与 tv_* 的几何估算版互补：
# tv_* 零依赖但对齐 TV 原版算法，这批是 WS 实采的逐笔方向/挂单存活/撤单率。
from .orderflow_tools import (  # noqa: E402
    ORDERFLOW_TOOL_DEFS as _ORDERFLOW_TOOL_DEFS,
    ORDERFLOW_TOOL_NAMES,
    _orderflow_db,
    run_orderflow_tool,
)

NATIVE_TOOLS.extend(_ORDERFLOW_TOOL_DEFS)

# 真 Delta / CVD —— 用交易所**实测** taker 量，而非 K 线几何估算
from .taker_delta import summarize as _taker_summarize  # noqa: E402

_TAKER_DELTA_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "taker_delta",
        "description": (
            "Real taker delta and cumulative delta (CVD) from exchange-reported taker "
            "buy/sell volume (Gate /contract_stats: long_taker_size − short_taker_size). "
            "Unlike tv_cdv — which ESTIMATES delta from candle geometry — these are "
            "measured values. Prefer this over tv_cdv wherever it covers the timeframe. "
            "Returns `delta_last`/`cvd_last` plus tails; CVD is more meaningful than a "
            "single bar (a single bar only shows the dynamic shift, the cumulative value "
            "carries the limited predictive edge). Also returns `taker` "
            "(long_taker_size / short_taker_size / lsr_taker), large-holder positioning "
            "(top_lsr_size, top_long_size/top_short_size, long_users/short_users), "
            "funding rate, and liquidation sizes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "币种（取【品种宇宙】里的值）"},
                "tf": {"type": "string",
                       "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
                       "description": "aggregation interval (default 5m)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                          "description": "periods to fetch (default 60)"},
                "tail": {"type": "integer", "minimum": 1, "maximum": 50,
                         "description": "how many recent points to return (default 10)"},
            },
            "required": ["symbol"],
        },
    },
}
NATIVE_TOOLS.append(_TAKER_DELTA_TOOL_DEF)


# 依赖 pa-data-source `aux_cache.db` 的工具。数据源不在时它们**每次调用只会返回
# `aux_cache.db not found`** —— 实测实盘：trades_flow 19/19、market_stats 1/1、
# liquidations 1/1 全失败，占累计 286 次调用的 7.3%，模型白烧轮次。
AUX_TOOL_NAMES = (
    "trades_flow", "liquidations", "market_stats", "tech_analysis", "coin_info",
    "onchain", "social", "overview", "sentiment", "macro",
)


def available_native_tools(bot_root=None) -> list:
    """按数据源可用性过滤后的工具 schema 列表。

    数据源（`aux_cache.db`）不在时**不把那 10 个 aux 工具挂给模型** —— 模型看不到
    就调不到，自然不会再白烧轮次。这是「AI 可见的工具面必须与系统实际能提供的一致」
    那条原则的直接落地（同 HealthMonitor 装饰性、AI 预算 vs 闸门那一类）。

    订单流工具同理：`orderflow.db` 不在（采集未启用）时不挂。
    """
    out = NATIVE_TOOLS
    if _aux_db(bot_root) is None:
        out = [t for t in out
               if (t.get("function") or {}).get("name") not in AUX_TOOL_NAMES]
    if _orderflow_db(bot_root) is None:
        out = [t for t in out
               if (t.get("function") or {}).get("name") not in ORDERFLOW_TOOL_NAMES]
    return out


# 元工具：始终保留。它们是加载 skill 方法论 / 读 references 的唯一入口，
# 被白名单挡掉等于把 SkillKit 弄坏。
META_TOOL_NAMES = ("skill", "skill_ref")

# SMC 工具的内部预热根数。原版在 TV 上 `ta.atr(200)` 始终有值、且结构状态机
# （start/bos/choch/loc/main）是全局累积的。只喂模型要的 limit 根会让 ATR 降级
# （实测 limit=120 → ATR=31.8，2000 根 → 76.1）、状态机落在不同分支 —— 同一段
# 区间内两者结论不同。实测 300 根即收敛（limit=300 与 2000 逐条一致），取 500 留余量。
_SMC_WARMUP_BARS = 500


def _tool_allowed(name: str, allow=None, deny=None) -> bool:
    """bot 级工具策略的**唯一判据**。`filter_tool_schemas` 与 `run_tool` 都用它。

    两处必须同源：只过滤 schema 是**不够**的 —— 模型会凭空报出不在 schema 里的
    工具名，实测 5 轮里 1 轮出现 `sqzmom` / `tv_rsi_yata`（白名单只放 3 个工具）。
    schema 负责「看不到」，这里负责「调不到」。
    """
    import fnmatch

    if name in META_TOOL_NAMES:
        return True
    allow_pats = [p for p in (allow or []) if str(p).strip()]
    deny_pats = [p for p in (deny or []) if str(p).strip()]
    if allow_pats and not any(fnmatch.fnmatch(name, str(p)) for p in allow_pats):
        return False
    if deny_pats and any(fnmatch.fnmatch(name, str(p)) for p in deny_pats):
        return False
    return True


def filter_tool_schemas(schemas: list, allow=None, deny=None) -> list:
    """按 bot 配置的 `strategist.tools.allow` / `.deny` 过滤工具 schema。

    两者都支持 fnmatch 通配（如 `tv_*`、`smc_*`）。语义：

      - `allow` 非空 → **只保留命中的**（元工具始终保留）
      - `deny` → 在 allow 之后**再移除命中的**（deny 优先）
      - 都不给 → 原样返回（向后兼容）

    为什么过滤 schema 而不是只拦调用：模型**看不到**就**调不到** ——
    这比事后拒绝更彻底，也不会浪费一个轮次。与 `available_native_tools`
    的「数据源不在就不挂」是同一条原则。
    **但它不能替代调用点的检查** —— 见 `_tool_allowed` 的说明。
    """
    return [
        t for t in schemas
        if _tool_allowed((t.get("function") or {}).get("name") or "", allow, deny)
    ]


def validate_tool_policy(allow=None, deny=None, known=None) -> None:
    """`allow` / `deny` 里出现匹配不到任何工具的模式 → 抛错。

    为什么要抛错而不是忽略：本项目的教训是**静默失效** ——
    一个拼错的 `deny: [sqzmo]` 会**静默地什么都不禁**，而配置看起来是生效的
    （同 HealthMonitor 装饰性、触发器参数越界那几类）。宁可启动就炸。
    """
    import fnmatch

    known = list(known if known is not None else TOOL_NAMES)
    bad = []
    for pats, label in ((allow, "allow"), (deny, "deny")):
        for p in (pats or []):
            p = str(p).strip()
            if p and not any(fnmatch.fnmatch(n, p) for n in known):
                bad.append("%s: %r" % (label, p))
    if bad:
        raise ValueError(
            "strategist.tools 里有匹配不到任何工具的模式（拼错了？）："
            + ", ".join(bad)
            + "。可用工具：" + ", ".join(sorted(known))
        )








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


    allow: Optional[list] = None,


    deny: Optional[list] = None,
    # 【品种宇宙】：工具的 symbol 缺省策略要用它（见 strategist/symbols.py）
    symbols: Optional[list] = None,


) -> Any:


    """Execute one market tool. Returns JSON-serializable result."""


    name = str(name or "").strip().lower()


    args = dict(args or {})


    if name not in TOOL_NAMES:


        return {"error": f"unknown tool {name!r}", "allowed": list(TOOL_NAMES)}


    # bot 级策略：**必须在调用点再拦一次**。只过滤 schema 不够 ——
    # 模型会凭空报出不在 schema 里的工具名（实测 5 轮里 1 轮出现 sqzmom/tv_rsi_yata，
    # 而白名单只放了 3 个工具）。schema 管「看不到」，这里管「调不到」。
    if not _tool_allowed(name, allow, deny):


        return {
            "error": f"tool {name!r} is not enabled for this bot",
            # 把**可用工具**直接列出来，减少模型的盲目重试（实测它会连着猜好几次）
            "available": [n for n in TOOL_NAMES if _tool_allowed(n, allow, deny)],
            "hint": "该工具被 strategist.tools 的 allow/deny 关掉了；请只用 available 里的工具",
        }


    # journal_lookup: 记忆检索（不需要 gate client，只要 bot_root / bot_id）
    if name == "journal_lookup":
        from pathlib import Path as _P

        cid = str(args.get("cycle_id") or "").strip()
        if not cid:
            return {"error": "cycle_id is required",
                    "hint": "用 prompt 里 [近期决策索引] 列出的 cycle_id"}
        if not bot_root or not bot_id:
            return {"error": "journal unavailable: no bot context"}
        try:
            from ..memory.journal import MemoryJournal
            rec = MemoryJournal(_P(bot_root), str(bot_id)).find(cid)
        except Exception as e:  # noqa: BLE001
            return {"error": f"journal read failed: {e}"[:160]}
        if rec is None:
            return {"error": f"no journal record for cycle_id {cid!r}",
                    "hint": "可能已被遗忘 GC 归档；请用 [近期决策索引] 里列出的 cycle_id"}
        return {
            "cycle_id": rec.get("cycle_id", ""),
            "ts": rec.get("ts"),
            "decision": rec.get("decision", ""),
            "reasoning": rec.get("reasoning", ""),
            "executed": rec.get("executed"),
            "exec_result": rec.get("exec_result") or {},
            "memory_refs": rec.get("memory_refs") or [],
            # Tier 1 字段：跨轮一致性的依据（契约要求填了就要能回看）
            "region": rec.get("region"),
            "invalidation_price": rec.get("invalidation_price"),
            "time_stop_bars": rec.get("time_stop_bars"),
        }

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
                               market_cfg=market_cfg, symbols=symbols)


        if name in ORDERFLOW_TOOL_NAMES:
            return run_orderflow_tool(bot_root, name, args)


        if name == "klines":


            sym = str(args.get("symbol") or args.get("sym") or "")


            tf = str(args.get("tf") or args.get("interval") or "15m").lower()


            limit = max(1, min(int(args.get("limit") or 50), 200))


            res = resolve_candles(client, sym, tf, limit, market_cfg=market_cfg, env=env, bot_root=bot_root)


            wanted = _resolve_wanted(market_cfg.indicators if market_cfg else None)

            rows = attach_indicators(list(res.rows), wanted)


            return {


                "symbol": sym,


                "tf": tf,


                "source": res.source,


                "stale": res.stale,


                "rows": [_strip_carried_indicators(r, wanted) for r in rows[-limit:]],


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


                "rows": [_strip_carried_indicators(r, names) for r in rows[-limit:]],


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


            interval = str(args.get("interval") or args.get("tf") or "")


            lim = max(1, min(int(args.get("limit") or 1), 200))


            rows = client.get_contract_stats(sym, limit=lim, interval=interval) or []


            if not rows:

                return {"symbol": sym, "note": "no stats"}


            # 真 Delta 就藏在 taker 量里；顺手算出来，省得模型自己翻字段做减法

            latest = dict(rows[-1])

            lt, st = latest.get("long_taker_size"), latest.get("short_taker_size")

            if lt is not None and st is not None:

                try:

                    latest["taker_delta"] = float(lt) - float(st)

                except (TypeError, ValueError):

                    pass


            if lim == 1:

                return latest


            return {"symbol": sym, "interval": interval or "default", "stats": rows}




        if name == "taker_delta":


            sym = str(args.get("symbol") or args.get("sym") or "")


            tf = str(args.get("tf") or args.get("interval") or "5m").lower()


            lim = max(1, min(int(args.get("limit") or 60), 500))


            tail = max(1, min(int(args.get("tail") or 10), 50))


            rows = client.get_contract_stats(sym, limit=lim, interval=tf) or []


            if not rows:

                return {"symbol": sym, "tf": tf, "error": "no contract_stats rows"}


            rows = sorted(rows, key=lambda x: x.get("time") or 0)


            out = _taker_summarize(rows, tail=tail)


            return {"symbol": sym, "tf": tf, **out}





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


            # aux 里存的是哪种形态实测为准：`fetch_aux.py` 写的是**带下划线的合约名**
            # （`BTC_USDT`），历史上/别处也可能是 `BTCUSDT` 或 `BTC`。三种都试 ——
            # 少试一种就是「查不到却静默返回空」，比报错更难发现。


            base = sym.replace("USDT", "")


            keys = [k for k in (sym, f"{base}_USDT", base) if k] if sym else []


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





            sym, _sym_note = resolve_symbol_arg(args, symbols, tool="smc_map")
            if not sym:
                return symbol_error_payload(_sym_note, symbols, tool="smc_map")



            tf = str(args.get("tf") or args.get("interval") or "15m").lower()


            lim = max(30, min(int(args.get("limit") or 150), 300))


            # same hybrid path as klines: per-exchange local DB → that venue's REST


            fetch = max(lim, _SMC_WARMUP_BARS)


            res = resolve_candles(client, sym, tf, fetch, market_cfg=market_cfg, env=env, bot_root=bot_root)


            rows = list(res.rows or [])


            out = smc_map_summary(compute_smc_map(rows))


            out.update({


                "symbol": sym, "tf": tf, "n": len(rows),


                "limit_requested": lim, "bars_analyzed": len(rows),


                "source": res.source, "stale": res.stale, "degraded": res.degraded,


                "set": "map",


            })


            return out





        if name == "smc_events":


            from .smc_events import compute_smc_events, smc_events_summary





            sym, _sym_note = resolve_symbol_arg(args, symbols, tool="smc_events")
            if not sym:
                return symbol_error_payload(_sym_note, symbols, tool="smc_events")



            tf = str(args.get("tf") or args.get("interval") or "15m").lower()


            lim = max(30, min(int(args.get("limit") or 150), 300))


            fetch = max(lim, _SMC_WARMUP_BARS)


            res = resolve_candles(client, sym, tf, fetch, market_cfg=market_cfg, env=env, bot_root=bot_root)


            rows = list(res.rows or [])


            out = smc_events_summary(compute_smc_events(rows))


            out.update({


                "symbol": sym, "tf": tf, "n": len(rows),


                "limit_requested": lim, "bars_analyzed": len(rows),


                "source": res.source, "stale": res.stale, "degraded": res.degraded,


                "set": "events",


            })


            return out





        if name == "sqzmom":


            





            sym, _sym_note = resolve_symbol_arg(args, symbols, tool="sqzmom")
            if not sym:
                return symbol_error_payload(_sym_note, symbols, tool="sqzmom")



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


