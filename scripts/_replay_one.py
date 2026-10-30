# -*- coding: utf-8 -*-
"""单版本 K 线回放：给定 repo root + 数据 + 区间，输出逐轮轨迹 JSON。

用法：python _replay_one.py <root> <data.json> <start> <end> <out.json>

每根 15m：
  1) 用本根 low/high 撮合**上一轮**留下的挂单与保护单（真穿越成交）
  2) 收盘后跑 `_complete_ladder`（该版本代码）
  3) 记录 side / 阶梯 / 持仓 / 权益
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
DATA = Path(sys.argv[2])
START, END = int(sys.argv[3]), int(sys.argv[4])
OUT = Path(sys.argv[5])
MODE = sys.argv[6] if len(sys.argv) > 6 else "legacy"   # legacy=线上现有行为 / structure=新规则
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

QUANTO = 0.01
FEE = 0.0002
NOTIONAL_CAP = float(os.environ.get("REPLAY_NOTIONAL_CAP") or 1400.0)  # 生产 max_total_notional_usd，回放里模拟风控拒单


class Meta:
    quanto_multiplier = QUANTO
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self):
        self.mark = 0.0
        self.book = []        # 入场挂单 {id, price, size}
        self.prot = []        # 保护单 {id, initial:{size}, trigger:{price,rule}}
        self.pos = 0.0        # 带符号张数
        self.avg = 0.0
        self.fills = []       # 成交记录
        self.sl_hits = []
        self._n = 0

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if abs(self.pos) < 1e-9:
            return []
        return [{"contract": "ETH_USDT", "side": "long" if self.pos > 0 else "short",
                 "size": str(self.pos), "entry_price": str(self.avg)}]

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        px, sz = float(body["price"]), float(body["size"])
        gross = (abs(self.pos) + sum(abs(b["size"]) for b in self.book)
                 + abs(sz)) * self.mark * QUANTO
        if gross > NOTIONAL_CAP:
            raise RuntimeError(f"notional_cap {gross:.0f}>{NOTIONAL_CAP:.0f}")
        rec = {"id": f"o{self._n}", "price": px,
               "size": sz}
        self._n += 1
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        rec = {"id": f"p{self._n}", "status": "open",
               "initial": body["initial"], "trigger": body["trigger"]}
        self._n += 1
        self.prot.append(rec)
        return {"id": rec["id"]}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, oid):
        self.prot = [p for p in self.prot if p["id"] != oid]
        return {}


def match_bar(c: Client, bar, idx):
    """用本根 K 线撮合：先保护单（止损优先），再入场挂单。"""
    lo, hi = bar["l"], bar["h"]
    # ① 保护单：多头 rule=2 跌破卖出 / 空头 rule=1 涨破买入
    for p in list(c.prot):
        trg = p.get("trigger") or {}
        try:
            px = float(trg.get("price") or 0)
        except (TypeError, ValueError):
            continue
        rule = int(trg.get("rule") or 0)
        if px <= 0 or abs(c.pos) < 1e-9:
            continue
        try:
            cap = abs(float((p.get("initial") or {}).get("size") or 0)) or abs(c.pos)
        except (TypeError, ValueError):
            cap = abs(c.pos)
        if rule == 2 and c.pos > 0 and lo <= px:
            qty = min(cap, c.pos)
            pnl = qty * (px - c.avg) * QUANTO
            c.pos -= qty
            if abs(c.pos) < 1e-9:
                c.pos, c.avg = 0.0, 0.0
        elif rule == 1 and c.pos < 0 and hi >= px:
            qty = min(cap, abs(c.pos))
            pnl = qty * (c.avg - px) * QUANTO
            c.pos += qty
            if abs(c.pos) < 1e-9:
                c.pos, c.avg = 0.0, 0.0
        else:
            continue
        # 触发后该单消失（否则同一张 SL 每根 K 线重复触发 —— 实测把 SL 次数放大 10 倍）
        c.prot.remove(p)
        c.sl_hits.append({"i": idx, "price": px, "qty": round(qty, 2),
                          "pnl": round(pnl, 3)})
    # ② 入场挂单：买单 low<=px 成交；卖单 high>=px 成交
    for b in list(c.book):
        px, sz = float(b["price"]), float(b["size"])
        hit = (sz > 0 and lo <= px) or (sz < 0 and hi >= px)
        if not hit:
            continue
        prev_pos, prev_avg = c.pos, c.avg
        new_pos = c.pos + sz
        if abs(new_pos) > 1e-9 and (prev_pos == 0 or (prev_pos > 0) == (new_pos > 0)):
            c.avg = (abs(prev_pos) * prev_avg + abs(sz) * px) / abs(new_pos)
        elif abs(new_pos) < 1e-9:
            c.avg = 0.0
        else:
            c.avg = px
        c.pos = new_pos
        c.fills.append({"i": idx, "price": px, "size": sz})
        c.book.remove(b)


def _smc_extract(win, mark, cand):
    """对一段 K 线跑 smc_map，把 OB/FVG/EQL·EQH/pivot 价位塞进 cand。"""
    from omnialpha.strategist.smc_map import compute_smc_map, smc_map_summary
    if len(win) < 30:
        return
    rows = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
             "c": b["c"], "v": b.get("v") or 0} for b in win]
    summary = smc_map_summary(compute_smc_map(rows))

    def _add(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if v > 0 and 0.6 < v / mark < 1.6:
            cand.add(round(v, 2))

    for grp in ("swing", "internal"):
        for ob in (summary.get("order_blocks") or {}).get(grp) or []:
            _add(ob.get("low"))
            _add(ob.get("high"))
    for f in summary.get("fvgs") or []:
        _add(f.get("top"))
        _add(f.get("bottom"))
    for key in ("equal_lows", "equal_highs"):
        for e in summary.get(key) or []:
            _add(e.get("price") if isinstance(e, dict) else e)
    for pv in summary.get("swing_pivots") or []:
        _add(pv.get("price") if isinstance(pv, dict) else pv)
    _add(min(x["l"] for x in win))
    _add(max(x["h"] for x in win))


def synth_tf(bars, span):
    """15m → N×15m（span 根合一根）。只聚合已发生的 bar，回放里不引入未来数据。"""
    out: list = []
    cur = None
    width = span * 900
    for b in bars:
        slot = b["t"] - (b["t"] % width)
        if cur is not None and cur["t"] == slot:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]
        else:
            if cur is not None:
                out.append(cur)
            cur = {"t": slot, "o": b["o"], "h": b["h"], "l": b["l"],
                   "c": b["c"], "v": b.get("v") or 0}
    if cur is not None:
        out.append(cur)
    return out


def synth_h4(bars, span=16):
    """15m → 4h（16 根合一根）。只从已发生的 bar 聚合，回放里不会引入未来数据。"""
    out: list = []
    cur = None
    for b in bars:
        slot = b["t"] - (b["t"] % (span * 900))
        if cur is not None and cur["t"] == slot:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]
        else:
            if cur is not None:
                out.append(cur)
            cur = {"t": slot, "o": b["o"], "h": b["h"], "l": b["l"],
                   "c": b["c"], "v": b.get("v") or 0}
    if cur is not None:
        out.append(cur)
    return out


def smc_levels(bars, i, lookback=200, h4=None):
    """**真实 smc_map** 提取结构位（OB 上下沿 / FVG 边界 / EQL·EQH / swing pivot）。

    与 `struct_levels` 的区别：那个只用 swing 极值 + 区间上下沿（≈等距的变体），
    这个走引擎线上真正在用的 `compute_smc_map`，代表「全部使用结构位」的样子。
    """
    mark = bars[i]["c"]
    cand: set = set()
    _smc_extract(bars[max(0, i - lookback + 1): i + 1], mark, cand)
    # 多周期：线上模型查 15m/1h/4h/1d。回放数据里的 h4 只有 c/ema200（没有 OHLC），
    # 所以这里**从 15m 合成 4h**（只用 <= i 的 bar，不给未来函数）。
    h4o = synth_h4(bars[: i + 1])
    if len(h4o) >= 30:
        _smc_extract(h4o[-120:], mark, cand)
    return sorted(cand, reverse=True)


def struct_levels(bars, i, lookback=96):
    """近 lookback 根的 swing 极值 + 区间上下沿（模拟 AI 的 structure_levels）。"""
    win = bars[max(0, i - lookback + 1): i + 1]
    lv = []
    for k in range(2, len(win) - 2):
        h, l = win[k]["h"], win[k]["l"]
        if h >= max(x["h"] for x in win[k-2:k]) and h >= max(x["h"] for x in win[k+1:k+3]):
            lv.append(h)
        if l <= min(x["l"] for x in win[k-2:k]) and l <= min(x["l"] for x in win[k+1:k+3]):
            lv.append(l)
    if win:
        lv.append(min(x["l"] for x in win))
        lv.append(max(x["h"] for x in win))
    return sorted({round(v, 2) for v in lv})


def equity(c: Client):
    if abs(c.pos) < 1e-9:
        return 0.0
    return c.pos * (c.mark - c.avg) * QUANTO


def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    bars = data["bars"]
    h4 = data.get("h4") or (data.get("mtf") or {}).get("4h") or []
    MTF = data.get("mtf") or {}       # 真实 1h/4h/1d（没有则空，走合成）

    dbf = Path(tempfile.mkdtemp()) / "k.db"
    con = sqlite3.connect(dbf)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
    con.execute("INSERT INTO kline VALUES ('ETH_USDT','15m',0,1,0,0,0,0,8,0,0,0,NULL,0,0)")
    con.execute("INSERT INTO kline VALUES ('ETH_USDT','4h',0,1,0,0,0,0,NULL,NULL,NULL,NULL,0,NULL,NULL)")
    con.commit()

    # tpl 直接取**生产配置**（2026-10-30 起），否则 A/B 比的是几个月前的参数
    from omnialpha.config import load_bot_config
    tpl = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
    tpl.update({
        "martin": [], "snap_tol_pct": 0.15, "always_on": True, "symbol": "ETH_USDT",
        "anchor_mode": "fixed", "reanchor_threshold_pct": 2.0,
        "anchor_state_file": str(Path(tempfile.mkdtemp()) / "a.json"),
        "levels_state_file": str(Path(tempfile.mkdtemp()) / "l.json"),
        # 回放没有模型信号 → 统一用 auto（A/B 同口径，不引入方向差异）
        "direction_mode": "auto",
        "direction_hysteresis": {"enabled": True, "interval": "4h",
                                 "long_keep_ratio": 0.98, "short_keep_ratio": 1.005,
                                 "state_file": str(Path(tempfile.mkdtemp()) / "d.json")},
        "dynamic": dict(tpl.get("dynamic") or {}, db_path=str(dbf)),
    })
    # OVR_SPAN=8 → 覆盖整梯跨度上限（%），用来测「结构位被跨度约束」的效果
    _sp = os.environ.get("OVR_SPAN")
    if _sp:
        tpl["dynamic"] = dict(tpl.get("dynamic") or {}, max_span_pct=float(_sp))

    # OVR_LOTS=v3 → 复现旧版仓位（10 层全 1 张、马丁 3/7/10 各 1 张）
    if (os.environ.get("OVR_LOTS") or "").lower() == "v3":
        tpl["grid_lots"] = 1
        tpl["martin_slots"] = {3: 1, 7: 1, 10: 1}
        tpl["martin_slots_by_index"] = False

    if MODE in ("structure", "smc", "smc4", "mtf"):
        # 结构位模式：档位来自 structure_levels，不再等距
        # min_gap 取 `MIN_GAP` 环境变量（默认 0.66 = min_step，防止结构位密集时档距失控）
        # MIN_GAP=auto → 用当轮 ATR 动态档距（4.0 正式口径）；给数字则写死
        _mg = os.environ.get("MIN_GAP") or 0.66
        try:
            _mg = float(_mg)
        except (TypeError, ValueError):
            pass
        tpl["levels_dynamic"] = dict(tpl.get("levels_dynamic") or {},
                                     enabled=True, structure_only=True,
                                     min_gap_pct=_mg)
    else:
        tpl["levels_dynamic"] = dict(tpl.get("levels_dynamic") or {}, enabled=False)
    c = Client()
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(tpl))
    ex.ladder_template = dict(tpl)
    ex._round_meta = {}

    trace, prev_equity = [], 0.0
    closes = [b["c"] for b in bars]

    for i in range(START, END):
        bar = bars[i]
        c.mark = bar["c"]
        match_bar(c, bar, i)

        # 指标（线上版 auto 读 15m boll/ema20/ema50；本地版读 4h）
        w20 = closes[max(0, i - 19): i + 1]
        w50 = closes[max(0, i - 49): i + 1]
        boll = sum(w20) / len(w20)
        ema20 = sum(w20) / len(w20)
        ema50 = sum(w50) / len(w50)
        cur4 = None
        for rec in h4:
            if rec["t"] <= bar["t"]:
                cur4 = rec
            else:
                break
        c4 = cur4["c"] if cur4 else bar["c"]
        e4 = cur4["ema200"] if cur4 else bar["c"]

        # 指标：`_real_mtf.json`（服务器真实库导出）带 atr14/boll20/上下来，
        # 直接用真实值；旧数据（`_kline_data.json`）没有这些列才回退自算，
        # 否则「ATR 档距」比的是回放器的估算而不是线上真实的 ATR。
        use_atr = bar.get("atr")
        use_boll = bar.get("bb_mid")
        b_up = bar.get("bb_up") if bar.get("bb_up") else boll * 1.013
        b_lo = bar.get("bb_lo") if bar.get("bb_lo") else boll * 0.987
        con.execute("UPDATE kline SET t=?, o=?, h=?, l=?, c=?, atr14=?, boll20=?, "
                    "boll20_upper=?, boll20_lower=?, ema20=?, ema50=? WHERE interval='15m'",
                    (bar["t"], bar["o"], bar["h"], bar["l"], bar["c"],
                     use_atr if use_atr else 8.0,
                     use_boll if use_boll else boll, b_up, b_lo, ema20, ema50))
        con.execute("UPDATE kline SET t=?, c=?, ema200=? WHERE interval='4h'",
                    (cur4["t"] if cur4 else bar["t"], c4, e4))
        con.commit()

        # 引擎方向判定（即使本轮 already_full 不重铺，也记录它当前怎么判）
        auto_side, auto_note = None, {}
        try:
            auto_side, auto_note = ex._auto_direction(
                "ETH_USDT", c.mark, ex.ladder_template["dynamic"], ex.ladder_template)
        except TypeError:
            auto_side, auto_note = ex._auto_direction(
                "ETH_USDT", c.mark, ex.ladder_template["dynamic"])

        if MODE == "structure":
            ex._round_meta = {"structure_levels": struct_levels(bars, i, 96)}
        elif MODE == "smc":
            ex._round_meta = {"structure_levels": smc_levels(bars, i, 200)}
        elif MODE == "smc4":
            ex._round_meta = {"structure_levels": smc_levels(bars, i, 200, h4)}
        elif MODE == "mtf":
            # 4.0：**不喂 AI 结构位**，让引擎自己从 15m/1h/4h/1d 提取
            ex._round_meta = {}
            past = bars[: i + 1]
            if MTF:      # 真实 1h/4h/1d（服务器库导出）→ 只用 <= 当前 bar 的部分
                rows_mtf = {}
                for tf, arr in MTF.items():
                    j = len(arr) - 1
                    while j >= 0 and arr[j]["t"] > bar["t"]:
                        j -= 1
                    if j >= 0:
                        rows_mtf[tf] = arr[max(0, j - 259): j + 1]
                ex._smc_rows = dict(rows_mtf, **{"15m": past[-300:]})
            else:        # 老数据只有 15m → 合成（4h/1d 根数不足，多数轮次不参与）
                ex._smc_rows = {
                    "15m": past[-300:],
                    "1h": synth_tf(past, 4)[-240:],
                    "4h": synth_tf(past, 16)[-180:],
                    "1d": synth_tf(past, 96)[-120:],
                }
        else:
            ex._round_meta = {}

        rep = type("R", (), {"results": []})()
        ex._complete_ladder([Intent(action="hold", symbol="ETH_USDT")], rep)
        dets = [r.detail for r in rep.results
                if getattr(r, "action", "") == "ladder_template" and r.detail]
        det = next((d for d in dets if d.get("blocked") or "step_pct" in d or d.get("skipped")), {})
        dyn = det.get("dynamic") or {}
        side = dyn.get("side") or ("long" if c.pos > 0 else
                                  "short" if c.pos < 0 else None)
        prot = None
        if c.prot:
            trg = c.prot[-1].get("trigger") or {}
            prot = {"rule": int(trg.get("rule") or 0),
                    "price": round(float(trg.get("price") or 0), 1),
                    "size": round(float((c.prot[-1].get("initial") or {}).get("size") or 0), 1)}
        eq = equity(c)
        trace.append({
            "i": i, "t": bar["t"], "c": bar["c"], "side": side,
            "blocked": det.get("blocked"),
            "anchor": det.get("anchor"),
            "book": [[round(float(b["price"]), 2), round(float(b["size"]), 1)] for b in c.book],
            "pos": round(c.pos, 2), "avg": round(c.avg, 2),
            "equity": round(eq, 4), "pnl_delta": round(eq - prev_equity, 4),
            "prot": prot,
            "ema200_4h": round(e4, 1), "close4h": round(c4, 1),
            "levels": (dyn.get("levels") or {}).get("levels") or len(c.book),
            "sl_pts": (dyn.get("sl_points") or {}).get("points"),
            "sl_dist_pct": (dyn.get("sl_points") or {}).get("distance_pct"),
            "struct_note": dyn.get("structure_only"),
            "levels_dyn": dyn.get("levels"),
            "auto_side": auto_side, "auto_rule": (auto_note or {}).get("rule"),
            "auto_reason": ((auto_note or {}).get("reason") or "")[:44],
        })
        prev_equity = eq

    con.close()
    dbf.unlink(missing_ok=True)
    OUT.write_text(json.dumps({
        "trace": trace,
        "fills": c.fills, "sl_hits": c.sl_hits,
        "summary": {
            "realised": round(sum(h["pnl"] for h in c.sl_hits)
                              - sum(abs(f["size"]) * f["price"] * QUANTO * FEE
                                    for f in c.fills), 3),
            "sl_pnl": round(sum(h["pnl"] for h in c.sl_hits), 3),
            "fills": len(c.fills),
            "fill_qty": round(sum(abs(f["size"]) for f in c.fills), 1),
            "sl_hits": len(c.sl_hits),
            "final_pos": round(c.pos, 2),
            "final_equity": round(equity(c), 3),
            "side_flips": sum(1 for a, b in zip(trace, trace[1:])
                              if a["side"] and b["side"] and a["side"] != b["side"]),
            "auto_flips": sum(1 for a, b in zip(trace, trace[1:])
                              if a["auto_side"] and b["auto_side"]
                              and a["auto_side"] != b["auto_side"]),
            "blocked_rounds": sum(1 for x in trace if x["blocked"]),
        },
    }, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(OUT), "rounds": len(trace)}))


if __name__ == "__main__":
    main()
