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
    bars, h4 = data["bars"], data["h4"]

    dbf = Path(tempfile.mkdtemp()) / "k.db"
    con = sqlite3.connect(dbf)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
    con.execute("INSERT INTO kline VALUES ('ETH_USDT','15m',0,1,0,0,0,0,8,0,0,0,NULL,0,0)")
    con.execute("INSERT INTO kline VALUES ('ETH_USDT','4h',0,1,0,0,0,0,NULL,NULL,NULL,NULL,0,NULL,NULL)")
    con.commit()

    tpl = {
        "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
        "martin_slots": {3: 6, 7: 7, 10: 9}, "martin": [], "snap_tol_pct": 0.15,
        "always_on": True, "symbol": "ETH_USDT", "sl_pct": 5.0,
        "anchor_mode": "fixed", "reanchor_threshold_pct": 2.0,
        "anchor_state_file": str(Path(tempfile.mkdtemp()) / "a.json"),
        "direction_mode": "auto",
        "direction_hysteresis": {"enabled": True, "interval": "4h",
                                 "long_keep_ratio": 0.98, "short_keep_ratio": 1.005,
                                 "state_file": str(Path(tempfile.mkdtemp()) / "d.json")},
        "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                    "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                    "scale_martin": True, "max_martin_pct": 9.0, "db_path": str(dbf)},
    }
    if MODE == "structure":
        tpl["levels_dynamic"] = {"enabled": True, "interval": "15m", "lookback": 96,
                                 "min_levels": 10, "max_levels": 15,
                                 "structure_only": True, "min_gap_pct": 0.3}
        tpl["sl_distance"] = {"enabled": True, "min_points": 300, "max_points": 500,
                              "range_mult": 4.0}
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

        con.execute("UPDATE kline SET t=?, o=?, h=?, l=?, c=?, boll20=?, boll20_upper=?, "
                    "boll20_lower=?, ema20=?, ema50=? WHERE interval='15m'",
                    (bar["t"], bar["o"], bar["h"], bar["l"], bar["c"], boll,
                     boll * 1.013, boll * 0.987, ema20, ema50))
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
