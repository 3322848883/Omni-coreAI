"""导出真实 K 线样本 + TV 指标计算值，供人工上 TradingView 对照。

输出：
  verify_data/tv_manual_check.md   — 数值表（人读）
  verify_data/tv_inputs.json       — 输入 OHLC（脚本可复算）
  verify_data/tv_pine_check.pine   — 可直接贴进 TV Pine 编辑器的对照脚本
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.tv_indicators import (
    calc_slope,
    heikin_ashi,
    kama,
    linreg_channel,
    lr_ha_candles,
    rsi_base,
    t3_moving_average,
    trendlines,
    volatility_bands,
)
from omnialpha.strategist.tv_indicators.lr_ha_candles import _linreg_val

DB = ROOT / "pa-data-source" / "data" / "kline.db"
OUT = ROOT / "verify_data"
N_BARS = 60
SYMBOL = "BTC_USDT"
INTERVAL = "1h"


def fetch_bars() -> list[dict]:
    con = sqlite3.connect(str(DB))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(kline)")]
        print("kline columns:", cols)
        # 探测列名
        time_col = "ts" if "ts" in cols else ("open_time" if "open_time" in cols else "t")
        sym_col = "symbol" if "symbol" in cols else ("contract" if "contract" in cols else None)
        iv_col = "interval" if "interval" in cols else ("tf" if "tf" in cols else None)
        where, params = [], []
        if sym_col:
            where.append(f"{sym_col} = ?")
            params.append(SYMBOL)
        if iv_col:
            where.append(f"{iv_col} = ?")
            params.append(INTERVAL)
        sql = f"SELECT * FROM kline"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {time_col} DESC LIMIT {N_BARS}"
        rows = con.execute(sql, params).fetchall()
        names = cols
        bars = [dict(zip(names, r)) for r in rows]
        bars.reverse()
        return bars
    finally:
        con.close()


def pick(bars: list[dict], *keys) -> list:
    for k in keys:
        if k in bars[0]:
            return [b[k] for b in bars]
    raise KeyError(keys)


def ts_human(vals) -> list[str]:
    out = []
    for v in vals:
        if isinstance(v, (int, float)) and v > 1e12:
            out.append(datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))
        elif isinstance(v, (int, float)) and v > 1e9:
            out.append(datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))
        else:
            out.append(str(v))
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)
    bars = fetch_bars()
    if len(bars) < 30:
        raise SystemExit(f"only {len(bars)} bars in db")
    print(f"loaded {len(bars)} bars, first keys={list(bars[0])}")

    times = pick(bars, "ts", "open_time", "t", "timestamp", "time")
    opens = [float(x) for x in pick(bars, "o", "open")]
    highs = [float(x) for x in pick(bars, "h", "high")]
    lows = [float(x) for x in pick(bars, "l", "low")]
    closes = [float(x) for x in pick(bars, "c", "close")]
    tsh = ts_human(times)

    # ── 计算 ──
    length = 20
    rsi_len = 14
    rsi_v = rsi_base(closes, rsi_len)
    slope, avg, intercept = calc_slope(closes, length)
    ch = linreg_channel(closes, highs, lows, length=length,
                        upper_mult=2.0, lower_mult=2.0)
    lr = _linreg_val(closes, length, 0)
    ha = heikin_ashi(opens, highs, lows, closes)
    lrha = lr_ha_candles(opens, highs, lows, closes, length=9)
    t3v = t3_moving_average(closes, 5, 0.7)
    kamav = kama(closes, 10)
    vb = volatility_bands(highs, lows, closes, length=20)
    tl = trendlines(highs, lows, closes, opens, lookback=10)

    # 取若干关键 bar 的值
    marks = [len(closes) - 1, len(closes) - 2, len(closes) - 5, len(closes) - 10]
    marks = sorted(set(marks))

    payload = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "source": "pa-data-source/data/kline.db (Gate)",
        "n_bars": len(closes),
        "params": {
            "linreg_length": length, "rsi_length": rsi_len,
            "channel_mult": 2.0, "t3_len": 5, "t3_alpha": 0.7,
            "kama_period": 10, "vol_bands_len": 20, "lrha_len": 9,
        },
        "bars": {
            "time_utc": tsh,
            "open": opens, "high": highs, "low": lows, "close": closes,
        },
        "expected": {
            "rsi": [None if v is None else round(v, 6) for v in rsi_v],
            "linreg": [None if v is None else round(v, 6) for v in lr],
            "t3": [None if v is None else round(v, 6) for v in t3v],
            "kama": [None if v is None else round(v, 6) for v in kamav],
            "ha_open": [None if v is None else round(v, 6) for v in ha["open"]],
            "ha_close": [None if v is None else round(v, 6) for v in ha["close"]],
            "lrha_close": [None if v is None else round(v, 6) for v in lrha["close"]],
            "vb_basis": [None if v is None else round(v, 6) for v in vb["basis"]],
            "vb_upper_inner": [None if v is None else round(v, 6) for v in vb["upper_inner"]],
            "calc_slope": {
                "slope": None if slope is None else round(slope, 10),
                "average": None if avg is None else round(avg, 6),
                "intercept": None if intercept is None else round(intercept, 6),
            },
            "linreg_channel_last": {
                "base_start": None if not ch["base"]["start"] else round(ch["base"]["start"], 6),
                "base_end": None if not ch["base"]["end"] else round(ch["base"]["end"], 6),
                "upper_start": None if not ch["upper"]["start"] else round(ch["upper"]["start"], 6),
                "upper_end": None if not ch["upper"]["end"] else round(ch["upper"]["end"], 6),
                "lower_start": None if not ch["lower"]["start"] else round(ch["lower"]["start"], 6),
                "lower_end": None if not ch["lower"]["end"] else round(ch["lower"]["end"], 6),
                "std_dev": None if ch["std_dev"] is None else round(ch["std_dev"], 6),
                "pearson_r": None if ch["pearson_r"] is None else round(ch["pearson_r"], 8),
            },
            "trendlines": tl,
        },
    }

    (OUT / "tv_inputs.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Markdown 数值表 ──
    lines = []
    lines.append(f"# TV 人工对照包 — {SYMBOL} {INTERVAL}")
    lines.append("")
    lines.append(f"数据来源：`{payload['source']}`，共 **{len(closes)}** 根 "
                 f"（{tsh[0]} ~ {tsh[-1]} UTC）。")
    lines.append("把下面 **OHLC** 贴进 TradingView 的 Pine 脚本（见 `tv_pine_check.pine`），")
    lines.append("或在 TV 图表上加载同参数指标后逐根核对 **期望值** 列。")
    lines.append("")
    lines.append("## 1. 关键期望值（最后几根）")
    lines.append("")
    lines.append("| bar | time UTC | close | RSI(14) | LinReg(20) | T3(5,0.7) | KAMA(10) | HA close | LRHA close | VB basis |")
    lines.append("|----:|----------|------:|--------:|-----------:|----------:|---------:|---------:|-----------:|---------:|")
    for i in marks:
        def fmt(v):
            return "—" if v is None else f"{v:.4f}"
        lines.append(
            f"| {i} | {tsh[i]} | {closes[i]:.2f} | {fmt(rsi_v[i])} | {fmt(lr[i])} "
            f"| {fmt(t3v[i])} | {fmt(kamav[i])} | {fmt(ha['close'][i])} "
            f"| {fmt(lrha['close'][i])} | {fmt(vb['basis'][i])} |"
        )
    lines.append("")
    lines.append("## 2. calc_slope / linreg_channel（长度 20，通道 ±2σ，窗口 = 最后 20 根）")
    lines.append("")
    lines.append(f"- slope = `{payload['expected']['calc_slope']['slope']}`")
    lines.append(f"- average = `{payload['expected']['calc_slope']['average']}`")
    lines.append(f"- intercept（窗口最老一根拟合价）= `{payload['expected']['calc_slope']['intercept']}`")
    lc = payload["expected"]["linreg_channel_last"]
    lines.append(f"- base.start = `{lc['base_start']}`  （窗口最老）")
    lines.append(f"- base.end   = `{lc['base_end']}`  （最新一根）")
    lines.append(f"- upper.start/end = `{lc['upper_start']}` / `{lc['upper_end']}`")
    lines.append(f"- lower.start/end = `{lc['lower_start']}` / `{lc['lower_end']}`")
    lines.append(f"- std_dev = `{lc['std_dev']}`   pearson_r = `{lc['pearson_r']}`")
    lines.append("")
    lines.append("## 3. RSI(14) 全序列（对照 TV ta.rsi）")
    lines.append("")
    lines.append("| bar | time UTC | close | RSI |")
    lines.append("|----:|----------|------:|----:|")
    for i in range(len(closes)):
        rv = "—" if rsi_v[i] is None else f"{rsi_v[i]:.4f}"
        lines.append(f"| {i} | {tsh[i]} | {closes[i]:.2f} | {rv} |")
    lines.append("")
    lines.append("## 4. 输入 OHLC（粘贴用，60 根）")
    lines.append("")
    lines.append("```text")
    lines.append("time_utc,open,high,low,close")
    for i in range(len(closes)):
        lines.append(f"{tsh[i]},{opens[i]:.4f},{highs[i]:.4f},{lows[i]:.4f},{closes[i]:.4f}")
    lines.append("```")
    lines.append("")
    lines.append("## 5. 在 TradingView 怎么验")
    lines.append("")
    lines.append("1. 打开 **Pine Editor** → 粘贴 `tv_pine_check.pine` → **Add to chart**。")
    lines.append("2. 图上 Data Window 会显示同名标签（rsi14 / linreg20 / t3 / kama / ha_close / lrha_close / vb_basis）。")
    lines.append("3. 把鼠标移到 **最后一根**，数值应与本表一致（小数点后 4 位）。")
    lines.append("4. 若要用真实 BTC 图表验：时间对齐 Gate 1h 收盘，个别根因交易所数据源不同可能有 0.0x 价差——")
    lines.append("   **算法一致性以粘贴脚本的固定数据为准**，图上比对用相对值（RSI、斜率符号、通道宽度比）。")
    lines.append("")

    (OUT / "tv_manual_check.md").write_text("\n".join(lines), encoding="utf-8")

    # ── Pine 脚本：硬编码同一批数据，全部指标从数组算（与图上 close 无关）──
    def series(vals, fmt="{:.4f}"):
        parts = [fmt.format(v) for v in vals]
        rows = [", ".join(parts[i:i + 10]) for i in range(0, len(parts), 10)]
        return ",\n     ".join(rows)

    pine = f"""//@version=5
// ============================================================================
// OmniAlpha ↔ TradingView 数值对照脚本
// 数据：{SYMBOL} {INTERVAL}  {tsh[0]} ~ {tsh[-1]} UTC（Gate kline.db，{len(closes)} 根）
// 用法：Pine Editor → 粘贴本文件 → Add to chart → 看右上角表格
// 全部指标从下方固定数组计算，与图表 close 无关，可贴在任意图上。
// 期望值见 verify_data/tv_manual_check.md
// ============================================================================
indicator("gbot TV check {SYMBOL} {INTERVAL}", overlay=true)

var float[] _o = array.from({series(opens)})
var float[] _h = array.from({series(highs)})
var float[] _l = array.from({series(lows)})
var float[] _c = array.from({series(closes)})

// ── RMA（Wilder，与 TV ta.rma 同）──
rma_arr(src, len) =>
    float[] out = array.new_float(array.size(src), na)
    if array.size(src) >= len
        float acc = 0.0
        for i = 0 to len - 1
            acc += array.get(src, i)
        acc /= len
        array.set(out, len - 1, acc)
        if array.size(src) > len
            for i = len to array.size(src) - 1
                acc := (acc * (len - 1) + array.get(src, i)) / len
                array.set(out, i, acc)
    out

// ── RSI(len)：首值 index=len，公式同 ta.rsi ──
rsi_from_arr(cl, len) =>
    int n = array.size(cl)
    float[] gains = array.new_float(n - 1, 0.0)
    float[] losses = array.new_float(n - 1, 0.0)
    if n > 1
        for i = 1 to n - 1
            float d = array.get(cl, i) - array.get(cl, i - 1)
            array.set(gains, i - 1, math.max(d, 0.0))
            array.set(losses, i - 1, math.max(-d, 0.0))
    float[] up = rma_arr(gains, len)
    float[] dn = rma_arr(losses, len)
    float[] out = array.new_float(n, na)
    for i = 0 to n - 1
        int gi = i - 1
        if gi >= 0 and not na(array.get(up, gi)) and not na(array.get(dn, gi))
            float u = array.get(up, gi)
            float d = array.get(dn, gi)
            float v = d == 0 ? 100.0 : u == 0 ? 0.0 : 100.0 - 100.0 / (1.0 + u / d)
            array.set(out, i, v)
    out

// ── ta.linreg(src, len, 0)：x=1..len，末点拟合值 ──
linreg_arr(src, len) =>
    int n = array.size(src)
    float[] out = array.new_float(n, na)
    float sumX = len * (len + 1) / 2.0
    float sumXX = len * (len + 1) * (2 * len + 1) / 6.0
    if n >= len
        for i = len - 1 to n - 1
            float sumY = 0.0
            float sumXY = 0.0
            for j = 0 to len - 1
                float v = array.get(src, i - len + 1 + j)
                sumY += v
                sumXY += (j + 1) * v
            float denom = len * sumXX - sumX * sumX
            float slope = (len * sumXY - sumX * sumY) / denom
            float intercept = (sumY - slope * sumX) / len
            array.set(out, i, intercept + slope * len)
    out

ema_arr(src, len) =>
    float k = 2.0 / (len + 1.0)
    float[] out = array.new_float(array.size(src), na)
    if array.size(src) > 0
        float prev = array.get(src, 0)
        array.set(out, 0, prev)
        if array.size(src) > 1
            for i = 1 to array.size(src) - 1
                prev := array.get(src, i) * k + prev * (1.0 - k)
                array.set(out, i, prev)
    out

gd_arr(src, len, a) =>
    float[] e1 = ema_arr(src, len)
    float[] e1f = array.new_float(array.size(e1), 0.0)
    for i = 0 to array.size(e1) - 1
        float v = array.get(e1, i)
        array.set(e1f, i, na(v) ? 0.0 : v)
    float[] e2 = ema_arr(e1f, len)
    float[] out = array.new_float(array.size(src), na)
    for i = 0 to array.size(src) - 1
        float v1 = array.get(e1, i)
        if not na(v1)
            array.set(out, i, v1 * (1.0 + a) - array.get(e2, i) * a)
    out

kama_arr(src, len) =>
    int n = array.size(src)
    float[] out = array.new_float(n, na)
    float fastest = 2.0 / 3.0
    float slowest = 2.0 / 31.0
    if n > len
        float prev = array.get(src, len - 1)
        array.set(out, len - 1, prev)
        for i = len to n - 1
            float chg = math.abs(array.get(src, i) - array.get(src, i - len))
            float vol = 0.0
            for j = i - len + 1 to i
                vol += math.abs(array.get(src, j) - array.get(src, j - 1))
            float er = vol > 0 ? chg / vol : 0.0
            float sc = math.pow(er * (fastest - slowest) + slowest, 2)
            prev := prev + sc * (array.get(src, i) - prev)
            array.set(out, i, prev)
    out

ha_close_arr(o, h, l, c) =>
    int n = array.size(c)
    float[] out = array.new_float(n, na)
    for i = 0 to n - 1
        array.set(out, i, (array.get(o, i) + array.get(h, i) + array.get(l, i) + array.get(c, i)) / 4.0)
    out

float[] rsiS = rsi_from_arr(_c, {rsi_len})
float[] linS = linreg_arr(_c, {length})
float[] t3g1 = gd_arr(_c, 5, 0.7)
float[] t3g2 = gd_arr(t3g1, 5, 0.7)
float[] t3g3 = gd_arr(t3g2, 5, 0.7)
float[] kamaS = kama_arr(_c, 10)
float[] haS = ha_close_arr(_o, _h, _l, _c)

int last = array.size(_c) - 1
float rsiLast = array.get(rsiS, last)
float linLast = array.get(linS, last)
float t3Last = array.get(t3g3, last)
float kamaLast = array.get(kamaS, last)
float haLast = array.get(haS, last)

calcSlope() =>
    int n = {length}
    float sumX = n * (n + 1) / 2.0
    float sumY = 0.0
    float sumXY = 0.0
    float sumXX = n * (n + 1) * (2 * n + 1) / 6.0
    for j = 0 to n - 1
        float v = array.get(_c, array.size(_c) - n + j)
        sumY += v
        sumXY += (j + 1) * v
    float slope = (n * sumXY - sumX * sumY) / (n * sumXX - sumX * sumX)
    float avg = sumY / n
    float intercept = avg - slope * sumX / n + slope
    [slope, avg, intercept, intercept + slope * (n - 1)]

[fSlope, fAvg, fInt, fEnd] = calcSlope()

if barstate.islast
    var table t = table.new(position.top_right, 2, 8, border_width=1, bgcolor=color.new(color.black, 20))
    table.cell(t, 0, 0, "metric", text_color=color.white, bgcolor=color.new(color.blue, 60))
    table.cell(t, 1, 0, "value (fixed data)", text_color=color.white, bgcolor=color.new(color.blue, 60))
    table.cell(t, 0, 1, "rsi{rsi_len}")
    table.cell(t, 1, 1, str.tostring(rsiLast, "#.####"), text_color=color.orange)
    table.cell(t, 0, 2, "linreg{length}")
    table.cell(t, 1, 2, str.tostring(linLast, "#.####"), text_color=color.aqua)
    table.cell(t, 0, 3, "t3(5,0.7)")
    table.cell(t, 1, 3, str.tostring(t3Last, "#.####"), text_color=color.purple)
    table.cell(t, 0, 4, "kama10")
    table.cell(t, 1, 4, str.tostring(kamaLast, "#.####"), text_color=color.yellow)
    table.cell(t, 0, 5, "ha_close")
    table.cell(t, 1, 5, str.tostring(haLast, "#.####"), text_color=color.green)
    table.cell(t, 0, 6, "base.end(ch{length})")
    table.cell(t, 1, 6, str.tostring(fEnd, "#.####"), text_color=color.white)
    table.cell(t, 0, 7, "slope")
    table.cell(t, 1, 7, str.tostring(fSlope, "#.##########"), text_color=color.white)
"""
    (OUT / "tv_pine_check.pine").write_text(pine, encoding="utf-8")

    print("\n=== 关键期望值 ===")
    print(f"bars: {len(closes)}  {tsh[0]} .. {tsh[-1]}")
    for i in marks:
        print(f"bar{i:3d} {tsh[i]}  close={closes[i]:.2f}  rsi={rsi_v[i] and f'{rsi_v[i]:.4f}'}"
              f"  linreg={lr[i] and f'{lr[i]:.4f}'}  t3={t3v[i] and f'{t3v[i]:.4f}'}"
              f"  kama={kamav[i] and f'{kamav[i]:.4f}'}")
    print(f"\ncalc_slope: slope={slope:.10f}  avg={avg:.6f}  intercept={intercept:.6f}")
    print(f"channel base.end={ch['base']['end']:.6f}  std={ch['std_dev']:.6f}  r={ch['pearson_r']:.8f}")
    print(f"\nwritten: {OUT/'tv_manual_check.md'}")
    print(f"written: {OUT/'tv_pine_check.pine'}")
    print(f"written: {OUT/'tv_inputs.json'}")


if __name__ == "__main__":
    main()
