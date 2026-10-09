"""Configurable event triggers for plan-loop (per-bot strategy).

Condition dict shapes (all optional fields documented in README):

  {"type": "kline_close"}                                  # handled by loop
  {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 20, "side": "above"|"below"}   # 状态
  {"type": "price_cross_ema", "symbol": "BTC_USDT", "period": 20, "dir": "up"|"down"|"any"}  # 事件
  {"type": "ema_stack", "symbol": "BTC_USDT", "fast": 20, "slow": 50, "dir": "bull"|"bear"}  # 状态
  {"type": "price_ema_dist", "symbol": "BTC_USDT", "period": 20, "pct": 1.5, "side": "above"|"below"}  # 状态
  {"type": "ema_slope", "symbol": "BTC_USDT", "period": 20, "bars": 3, "dir": "up"|"down"}   # 状态
  {"type": "ema_cross", "symbol": "BTC_USDT", "fast": 9, "slow": 21, "dir": "up"|"down"|"any"}
  {"type": "atr_spike", "symbol": "BTC_USDT", "period": 14, "mult": 1.5, "lookback": 20}
  {"type": "price_break", "symbol": "BTC_USDT", "lookback": 20, "side": "high"|"low"}
  {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "gt"|"lt", "level": 70}
  {"type": "ma_cross", "symbol": "BTC_USDT", "fast": 7, "slow": 30, "dir": "up"|"down"|"any", "ma": "sma"|"ema"}
  {"type": "macd_cross", "symbol": "BTC_USDT", "fast": 12, "slow": 26, "signal": 9, "dir": "up"|"down"|"any"}
  {"type": "boll_break", "symbol": "BTC_USDT", "period": 20, "k": 2, "side": "upper"|"lower"}
  {"type": "volume_spike", "symbol": "BTC_USDT", "mult": 2, "lookback": 20}

Each condition fires at most once per `cooldown_sec` (default 60) after it last fired,
**and by default only on the False→True edge** (see `check_conditions` — without the edge,
state-type conditions degenerate into a fixed-interval timer).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .indicators import atr, boll, ema, macd, rsi, sma
# symbol 归一与触发器白名单**同一份实现**（`trigger_store`）：同一条判据两处各写
# 一遍必然漂移，而这里的后果是「条件写在 `BTC` 上、执行器按 `BTC_USDT` 取数」。
from .trigger_store import normalize_symbol, normalize_symbols

log = logging.getLogger("omnialpha.triggers")

LEAF_CONDITIONS = frozenset({
    "price_vs_ema", "ema_cross", "atr_spike", "price_break", "rsi",
    "ma_cross", "macd_cross", "boll_break", "volume_spike",
})
COMBINATORS = frozenset({"all", "any"})
KNOWN_CONDITIONS = LEAF_CONDITIONS | COMBINATORS
MAX_COND_DEPTH = 2

# 条件未显式声明 `cooldown_sec` 时的默认冷却。
# 注意：这只对 **bot yaml 里声明的 conditions** 生效 —— AI 自设触发器由
# `AITrigger.to_condition()` 显式带上自己的 `cooldown_sec`（来自 policy），
# 走不到这个兜底。所以这里不该去读 AI policy 的默认值：两者语义不同。
DEFAULT_CONDITION_COOLDOWN_SEC = 60.0


class ConditionError(ValueError):
    pass


@dataclass
class ConditionState:
    last_fire: float = 0.0
    last_values: dict = field(default_factory=dict)
    # 上一次求值的判定结果 —— **边沿触发**靠它识别 False→True 那一刻。
    # 默认 False：进程刚起来时视为「尚未为真」，所以若此刻状态已成立会立刻报一次，
    # 这正是想要的（这个状态对进程而言是新的）。
    last_true: bool = False


def parse_conditions(
    raw: Optional[list],
    symbols: Optional[list] = None,
    _depth: int = 0,
    _path: str = "conditions",
) -> list[dict]:
    """Parse and validate conditions. Unknown types raise ConditionError.

    `symbols` 是**本 bot 的品种宇宙**（`cfg.symbols`）。叶条件缺 `symbol` 时的行为：

    | 宇宙 | 行为 |
    |---|---|
    | 1 个币 | **自动补**该币（唯一解，无歧义；单币 bot 行为不变） |
    | ≥2 个币 | **报错**（`ConditionError`，指明是哪个条件缺） |
    | 未提供（旧调用方） | 保持原样（symbol=""）—— 不凭空收紧已有调用 |

    为什么多币必须报错而不是留空：留空的条件在运行期只得到 `"no symbol"` →
    **永不触发**，启动不报错、运行不告警（B-11）。那正是「配置看起来生效、
    实际不生效」——唯一能把它变可见的手段是启动 fail-fast。
    """
    out = []
    for i, item in enumerate(raw or []):
        path = f"{_path}[{i}]"
        if not isinstance(item, dict):
            raise ConditionError("condition must be an object")
        ctype = str(item.get("type") or "").strip().lower()
        if ctype in ("", "kline_close"):
            continue
        if ctype not in KNOWN_CONDITIONS:
            raise ConditionError(
                f"unknown condition type {ctype!r}; supported: "
                f"{','.join(sorted(KNOWN_CONDITIONS))}"
            )
        item = dict(item)
        item["type"] = ctype
        item.setdefault("cooldown_sec", 60)
        if ctype in COMBINATORS:
            if _depth >= MAX_COND_DEPTH:
                raise ConditionError(f"condition nesting deeper than {MAX_COND_DEPTH}")
            children = item.get("children")
            if not isinstance(children, list) or not children:
                raise ConditionError(f"{ctype} requires non-empty children[]")
            item["children"] = parse_conditions(
                children, symbols, _depth=_depth + 1, _path=f"{path}.children"
            )
        else:
            symbol = normalize_symbol(item.get("symbol"))
            if not symbol:
                universe = normalize_symbols(symbols)
                if len(universe) == 1:
                    symbol = universe[0]
                elif len(universe) > 1:
                    raise ConditionError(
                        f"{path} ({ctype}) 缺少 symbol，而本 bot 有 {len(universe)} 个币"
                        f"（{','.join(universe)}）—— 缺 symbol 的条件永不触发，"
                        f"请在该条件里显式写明 symbol"
                    )
            item["symbol"] = symbol
        out.append(item)
    return out


def _load_candles(client, symbol: str, interval: str, limit: int) -> list[dict]:
    """取 K 线（升序）。

    必须走 fetch_rest_candles：它对新旧客户端都兼容（有 get_klines 用 get_klines，
    否则回退 public_get）。此前这里直接调 client.public_get，而客户端重构成
    ExchangeClient 后**没有该方法** → 每次条件求值都抛 AttributeError、被上层
    兜底成 False，导致 AI 自设触发器与条件事件**永远不会触发**（线上实测：
    178 个周期全部是 interval，cond[...] 出现 0 次）。
    """
    from .market import fetch_rest_candles

    rows = fetch_rest_candles(client, symbol, interval, limit)
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        out.append({
            "t": int(r.get("t") or 0),
            "v": float(r.get("v") or 0),
            "c": float(r.get("c") or 0),
            "h": float(r.get("h") or 0),
            "l": float(r.get("l") or 0),
            "o": float(r.get("o") or 0),
        })
    out.sort(key=lambda x: x["t"])
    return out


def evaluate_condition(client, cond: dict, timeframe: str, now: Optional[float] = None) -> tuple[bool, str]:
    """Return (fired, human reason)."""
    ts = now if now is not None else time.time()
    ctype = cond.get("type")

    if ctype in COMBINATORS:
        children = cond.get("children") or []
        if not children:
            return False, f"{ctype} empty"
        parts = []
        oks = []
        for ch in children:
            ok, reason = evaluate_condition(client, ch, timeframe, now=ts)
            oks.append(ok)
            parts.append(f"{ch.get('type')}:{'Y' if ok else 'N'}({reason[:40]})")
        joined = "; ".join(parts)
        if ctype == "all":
            return all(oks), f"all[{joined}]"
        return any(oks), f"any[{joined}]"

    symbol = cond.get("symbol") or ""
    if not symbol:
        # 缺 symbol 的条件**永不触发**（每次求值都到这里）—— 不能静默：
        # 配置错误必须留痕，否则「设了条件却从没被唤醒」在日志里查不出原因。
        log.warning("condition %s has no symbol → never fires (key=%s)",
                    ctype, cond.get("key") or "")
        return False, "no symbol"
    # ensure enough history for the longest indicator period
    period_hint = max(
        int(cond.get("period") or 20),
        int(cond.get("fast") or 9),
        int(cond.get("slow") or 21),
        int(cond.get("lookback") or 20) + 5,
    )
    limit = int(cond.get("candles") or max(80, period_hint + 30))
    try:
        rows = _load_candles(client, symbol, timeframe, max(limit, 30))
    except Exception as e:  # noqa: BLE001
        return False, f"candles error: {e}"
    if len(rows) < 5:
        return False, "not enough candles"

    closes = [r["c"] for r in rows]
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]
    last = closes[-1]

    if ctype == "price_vs_ema":
        period = int(cond.get("period") or 20)
        side = str(cond.get("side") or "above").lower()
        series = ema(closes, period)
        e = series[-1]
        if e is None:
            return False, "ema not ready"
        if side == "above":
            return last > e, f"last={last:.4f} ema{period}={e:.4f}"
        return last < e, f"last={last:.4f} ema{period}={e:.4f}"

    if ctype == "price_cross_ema":
        # 事件：**价格穿越** EMA（与 `price_vs_ema` 的区别就是「穿越」vs「在上下方」）。
        # 补这个类型的原因：原先 9 个类型里没有任何一个表达「价格穿 EMA」——
        # 模型想要这个语义时只能选 `price_vs_ema`（状态型），而状态型配冷却
        # 等于定时器（实测线上一个 bot 因此每 5 分钟被唤醒一次，占 43% 轮次）。
        period = int(cond.get("period") or 20)
        direction = str(cond.get("dir") or "any").lower()
        series = ema(closes, period)
        if len(closes) < 2 or None in (series[-1], series[-2]):
            return False, "ema not ready"
        now_up = last > series[-1]
        prev_up = closes[-2] > series[-2]
        crossed_up = now_up and not prev_up
        crossed_down = (not now_up) and prev_up
        if direction == "up" and crossed_up:
            return True, f"price cross up ema{period} last={last:.4f} ema={series[-1]:.4f}"
        if direction == "down" and crossed_down:
            return True, f"price cross down ema{period} last={last:.4f} ema={series[-1]:.4f}"
        if direction == "any" and (crossed_up or crossed_down):
            return True, (f"price cross {'up' if crossed_up else 'down'} ema{period} "
                          f"last={last:.4f} ema={series[-1]:.4f}")
        return False, f"last={last:.4f} ema{period}={series[-1]:.4f}"

    if ctype == "ema_stack":
        # 状态：价格 + 双 EMA 的**排列**（多头/空头）。趋势确认用。
        fast_n = int(cond.get("fast") or 20)
        slow_n = int(cond.get("slow") or 50)
        direction = str(cond.get("dir") or "bull").lower()
        f_s, s_s = ema(closes, fast_n), ema(closes, slow_n)
        if None in (f_s[-1], s_s[-1]):
            return False, "ema not ready"
        bull = last > f_s[-1] > s_s[-1]
        bear = last < f_s[-1] < s_s[-1]
        info = f"price={last:.4f} ema{fast_n}={f_s[-1]:.4f} ema{slow_n}={s_s[-1]:.4f}"
        if direction == "bull":
            return bull, info
        if direction == "bear":
            return bear, info
        return (bull or bear), info

    if ctype == "price_ema_dist":
        # 状态：价格**偏离** EMA 超过 pct%（乖离 / 超买超卖）。
        period = int(cond.get("period") or 20)
        side = str(cond.get("side") or "above").lower()
        try:
            pct = float(cond.get("pct") or 0.0)
        except (TypeError, ValueError):
            return False, "bad pct"
        series = ema(closes, period)
        e = series[-1]
        if e is None or e <= 0:
            return False, "ema not ready"
        dist = (last - e) / e * 100.0
        if pct <= 0:
            return False, "pct must be > 0"
        if side == "above":
            return dist >= pct, f"dist={dist:+.2f}% (need >= +{pct:g}%)"
        return dist <= -pct, f"dist={dist:+.2f}% (need <= -{pct:g}%)"

    if ctype == "ema_slope":
        # 状态：EMA 在最近 bars 根内**上行/下行**（趋势转向的早期信号）。
        period = int(cond.get("period") or 20)
        bars = int(cond.get("bars") or 3)
        direction = str(cond.get("dir") or "up").lower()
        series = ema(closes, period)
        if len(series) < bars + 1:
            return False, "not enough bars for ema_slope"
        a, b = series[-1], series[-1 - bars]
        if a is None or b is None:
            return False, "ema not ready"
        delta = a - b
        info = f"ema{period} {bars}bar delta={delta:+.4f}"
        if direction == "up":
            return delta > 0, info
        if direction == "down":
            return delta < 0, info
        return abs(delta) > 0, info

    if ctype == "ema_cross":
        fast_n = int(cond.get("fast") or 9)
        slow_n = int(cond.get("slow") or 21)
        direction = str(cond.get("dir") or "any").lower()
        if len(closes) < slow_n + 2:
            return False, "not enough for ema_cross"
        f_s = ema(closes, fast_n)
        s_s = ema(closes, slow_n)
        if None in (f_s[-1], f_s[-2], s_s[-1], s_s[-2]):
            return False, "ema not ready"
        now_up = f_s[-1] > s_s[-1]
        prev_up = f_s[-2] > s_s[-2]
        crossed_up = now_up and not prev_up
        crossed_down = (not now_up) and prev_up
        if direction == "up" and crossed_up:
            return True, f"ema{fast_n}>{slow_n} cross up"
        if direction == "down" and crossed_down:
            return True, f"ema{fast_n}<{slow_n} cross down"
        if direction == "any" and (crossed_up or crossed_down):
            return True, f"ema{fast_n}/{slow_n} cross {'up' if crossed_up else 'down'}"
        return False, f"fast={f_s[-1]:.4f} slow={s_s[-1]:.4f}"

    if ctype == "atr_spike":
        period = int(cond.get("period") or 14)
        mult = float(cond.get("mult") or 1.5)
        lookback = int(cond.get("lookback") or 20)
        series = atr(highs, lows, closes, period)
        if series[-1] is None:
            return False, "atr not ready"
        window = [x for x in series[-lookback - 1 : -1] if x is not None]
        if not window:
            return False, "atr window empty"
        avg = sum(window) / len(window)
        return series[-1] > avg * mult, f"atr={series[-1]:.4f} avg={avg:.4f} x{mult}"

    if ctype == "price_break":
        lookback = int(cond.get("lookback") or 20)
        side = str(cond.get("side") or "high").lower()
        window = closes[-lookback - 1 : -1]
        if len(window) < lookback:
            return False, "not enough lookback"
        if side == "high":
            hi = max(window)
            return last > hi, f"last={last:.4f} hi{lookback}={hi:.4f}"
        lo = min(window)
        return last < lo, f"last={last:.4f} lo{lookback}={lo:.4f}"

    if ctype == "rsi":
        period = int(cond.get("period") or 14)
        op = str(cond.get("op") or "gt").lower()
        level = float(cond.get("level") or 70)
        series = rsi(closes, period)
        val = series[-1]
        if val is None:
            return False, "rsi not ready"
        if op == "gt":
            return val > level, f"rsi{period}={val:.2f} > {level}"
        return val < level, f"rsi{period}={val:.2f} < {level}"

    if ctype == "ma_cross":
        fast_n = int(cond.get("fast") or 7)
        slow_n = int(cond.get("slow") or 30)
        direction = str(cond.get("dir") or "any").lower()
        kind = str(cond.get("ma") or "sma").lower()  # sma | ema
        if len(closes) < slow_n + 2:
            return False, "not enough for ma_cross"
        f_s = (ema if kind == "ema" else sma)(closes, fast_n)
        s_s = (ema if kind == "ema" else sma)(closes, slow_n)
        if None in (f_s[-1], f_s[-2], s_s[-1], s_s[-2]):
            return False, "ma not ready"
        now_up = f_s[-1] > s_s[-1]
        prev_up = f_s[-2] > s_s[-2]
        crossed_up = now_up and not prev_up
        crossed_down = (not now_up) and prev_up
        if direction == "up" and crossed_up:
            return True, f"ma{fast_n}>{slow_n} cross up"
        if direction == "down" and crossed_down:
            return True, f"ma{fast_n}<{slow_n} cross down"
        if direction == "any" and (crossed_up or crossed_down):
            return True, f"ma{fast_n}/{slow_n} cross {'up' if crossed_up else 'down'}"
        return False, f"fast={f_s[-1]:.4f} slow={s_s[-1]:.4f}"

    if ctype == "macd_cross":
        fast_n = int(cond.get("fast") or 12)
        slow_n = int(cond.get("slow") or 26)
        sig_n = int(cond.get("signal") or 9)
        direction = str(cond.get("dir") or "any").lower()
        series = macd(closes, fast_n, slow_n, sig_n)
        dif, dea = series["dif"], series["dea"]
        if None in (dif[-1], dif[-2], dea[-1], dea[-2]):
            return False, "macd not ready"
        now_up = dif[-1] > dea[-1]
        prev_up = dif[-2] > dea[-2]
        crossed_up = now_up and not prev_up
        crossed_down = (not now_up) and prev_up
        if direction == "up" and crossed_up:
            return True, f"macd cross up dif={dif[-1]:.4f} dea={dea[-1]:.4f}"
        if direction == "down" and crossed_down:
            return True, f"macd cross down dif={dif[-1]:.4f} dea={dea[-1]:.4f}"
        if direction == "any" and (crossed_up or crossed_down):
            return True, f"macd cross {'up' if crossed_up else 'down'}"
        return False, f"dif={dif[-1]:.4f} dea={dea[-1]:.4f}"

    if ctype == "boll_break":
        period = int(cond.get("period") or 20)
        kb = float(cond.get("k") or 2.0)
        side = str(cond.get("side") or "upper").lower()  # upper | lower
        series = boll(closes, period, kb)
        up, lo = series["upper"][-1], series["lower"][-1]
        if up is None or lo is None:
            return False, "boll not ready"
        if side == "upper":
            return last > up, f"last={last:.4f} boll_upper={up:.4f}"
        return last < lo, f"last={last:.4f} boll_lower={lo:.4f}"

    if ctype == "volume_spike":
        mult = float(cond.get("mult") or 2.0)
        lookback = int(cond.get("lookback") or 20)
        vols = [r["v"] for r in rows[-lookback - 1 : -1]]
        if not vols:
            return False, "volume window empty"
        avg = sum(vols) / len(vols)
        cur = rows[-1]["v"]
        return cur > avg * mult, f"vol={cur:.0f} avg={avg:.0f} x{mult}"

    return False, f"unknown condition {ctype}"


def check_conditions(
    client,
    conditions: list[dict],
    timeframe: str,
    states: Optional[dict] = None,
    now: Optional[float] = None,
) -> list[dict]:
    """Evaluate all conditions; return [{type, symbol, reason}] for newly fired ones.

    **边沿触发（默认）**：只在判定 **False → True** 那一刻报一次，状态持续期间不重复报。

    为什么必须这样：条件分两类 ——
      - **事件型**（`price_break` / `price_cross_ema` / `ema_cross` / `macd_cross` /
        `ma_cross` / `atr_spike` / `volume_spike` / `boll_break`）比较「现在 vs 上一根」，
        True 本身就是一瞬间。
      - **状态型**（`price_vs_ema` / `ema_stack` / `price_ema_dist` / `ema_slope` / `rsi`）
        只看「现在是否成立」，成立期间**每次求值都为真**。

    对状态型只按 `cooldown_sec` 去重，数学上等于一个定时器：价格持续在 EMA 一侧时
    每 5 分钟唤醒一轮。线上实测一个 bot 因此 196 分钟里被 `price_vs_ema` 唤醒 35 次、
    占全部轮次的 43%，2.5 小时烧掉约 1005 万 prompt token。

    加上边沿后，`price_vs_ema{side:above}` 的语义变成「价格**穿到** EMA 上方时叫醒我」——
    正是模型设它时想要的。想恢复旧的「持续成立就按冷却反复报」语义，
    在该条件里写 `edge_trigger: false`（bot yaml 声明 conditions 时可用）。
    """
    states = states if states is not None else {}
    ts = now if now is not None else time.time()
    fired = []
    for i, cond in enumerate(conditions):
        key = cond.get("key") or f"{cond.get('type')}:{cond.get('symbol')}:{i}"
        st: ConditionState = states.setdefault(key, ConditionState())
        cooldown = float(cond.get("cooldown_sec") or DEFAULT_CONDITION_COOLDOWN_SEC)
        if ts - st.last_fire < cooldown:
            continue
        ok, reason = evaluate_condition(client, cond, timeframe, now=ts)
        # 边沿触发：只在 **False → True** 那一刻报一次（见函数 docstring）。
        # 状态持续期间 `st.last_true` 一直是 True，所以不会每过冷却就重报一遍。
        _edge = cond.get("edge_trigger")
        edge = True if _edge is None else bool(_edge)
        if ok and (not edge or not st.last_true):
            st.last_fire = ts
            fired.append({
                "type": cond.get("type"),
                "symbol": cond.get("symbol") or (cond.get("children") or [{}])[0].get("symbol"),
                "reason": reason,
                "key": key,
            })
        st.last_true = ok
    return fired
