#!/usr/bin/env python3
"""挂单地图：把一轮真实 plan 的落地档位与「SMC 结构位 / 补点」逐档对账。

输出两样东西：
  1. 控制台表格（每档：价格 / 张数 / unit / 来源 / 命中周期 / TP / 距现价）
  2. `_charts/ladder_map.html`（自包含，垂直价格轴 + 双侧挂单 + SL/TP 标注）

用法：
  python scripts/_ladder_map.py --bot martin-grid-bi-gp \
      --plan C:/tmp/plan_snapshot.json --tfs 15m,1h,4h,1d
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omnialpha.strategist.smc_map import compute_smc_map, smc_map_summary  # noqa: E402

GATE = "https://api.gateio.ws/api/v4/futures/usdt"
BARS = {"5m": 500, "15m": 300, "1h": 240, "4h": 180, "1d": 120}
MIN_PER = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def http_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_klines(sym: str, tf: str, n: int) -> list[dict]:
    url = f"{GATE}/candlesticks?contract={sym}&interval={tf}&limit={n}"
    raw = http_json(url)
    rows = [{"t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
             "l": float(k["l"]), "c": float(k["c"]), "v": float(k.get("v") or 0)}
            for k in raw]
    rows.sort(key=lambda r: r["t"])
    return rows


def smc_levels(rows: list[dict], tf: str) -> dict[float, list[str]]:
    """一个周期的 SMC 价位 → {price: [kinds]}。"""
    summ = smc_map_summary(compute_smc_map(rows))
    out: dict[float, list[str]] = {}

    def add(px, kind):
        try:
            px = float(px)
        except (TypeError, ValueError):
            return
        if px <= 0:
            return
        out.setdefault(round(px, 2), []).append(kind)

    for grp in ("swing", "internal"):
        for ob in (summ.get("order_blocks") or {}).get(grp) or []:
            add(ob.get("low"), f"{grp}OB↓")
            add(ob.get("high"), f"{grp}OB↑")
    for f in summ.get("fvgs") or []:
        add(f.get("top"), "FVG↑")
        add(f.get("bottom"), "FVG↓")
    for key in ("equal_lows", "equal_highs"):
        for e in summ.get(key) or []:
            add(e.get("price") if isinstance(e, dict) else e,
                "EQL" if key == "equal_lows" else "EQH")
    for pv in summ.get("swing_pivots") or []:
        add(pv.get("price") if isinstance(pv, dict) else pv, "swing")
    add(min(r["l"] for r in rows), "区间低")
    add(max(r["h"] for r in rows), "区间高")
    return out


def load_state(bot: str) -> tuple[list, list, dict]:
    base = os.path.join(ROOT, "data", "bots", bot)
    table = []
    p = os.path.join(base, "state", "ladder_levels.json")
    if os.path.exists(p):
        table = json.load(open(p, encoding="utf-8")).get("levels") or []
    orders, sls = [], []
    db = os.path.join(ROOT, "data", "accounts", "gate-grid-paper", "account.db")
    if os.path.exists(db):
        con = sqlite3.connect(db)
        for price, size in con.execute("SELECT price, size FROM orders"):
            orders.append({"price": float(price), "size": float(size)})
        for trig, size, rule in con.execute(
                "SELECT trigger_price, size, rule FROM price_orders"):
            sls.append({"trigger": float(trig), "size": float(size), "rule": int(rule)})
        con.close()
    return table, orders, sls


def nearest(levels: dict[float, list[str]], px: float, tol_pct: float):
    best, best_d = None, 1e9
    for p in levels:
        d = abs(p - px) / px * 100
        if d <= tol_pct and d < best_d:
            best, best_d = p, d
    return (best, best_d) if best is not None else (None, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="martin-grid-bi-gp")
    ap.add_argument("--symbol", default="ETH_USDT")
    ap.add_argument("--plan", default="C:/tmp/plan_snapshot.json")
    ap.add_argument("--tfs", default="5m,15m,1h,4h")
    ap.add_argument("--tol", type=float, default=0.15, help="匹配 SMC 位的容差 %%")
    ap.add_argument("--out", default="_charts/ladder_map.html")
    a = ap.parse_args()

    tfs = [x.strip() for x in a.tfs.split(",") if x.strip()]
    tick = http_json(f"{GATE}/tickers?contract={a.symbol}")
    mark = float(tick[0]["mark_price"])

    # ── 各周期 SMC 位 ────────────────────────────────────────────
    per_tf: dict[str, dict[float, list[str]]] = {}
    for tf in tfs:
        try:
            rows = fetch_klines(a.symbol, tf, BARS.get(tf, 200))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {tf} K线失败: {e}")
            continue
        per_tf[tf] = smc_levels(rows, tf)
        print(f"  {tf}: {len(per_tf[tf])} 个 SMC 价位（{len(rows)} 根）")
    merged: dict[float, list[str]] = {}
    for tf, lv in per_tf.items():
        for p, kinds in lv.items():
            merged.setdefault(p, []).extend([f"{tf}:{k}" for k in kinds])

    plan = {}
    if os.path.exists(a.plan):
        plan = json.load(open(a.plan, encoding="utf-8"))
    ai_levels = [float(x) for x in ((plan.get("meta") or {}).get("structure_levels") or [])]

    table, orders, sls = load_state(a.bot)
    tp_of = {(x["side"], round(float(x["price"]), 2)): x for x in table}

    rows_out = []
    for o in orders:
        px, sz = o["price"], o["size"]
        side = "long" if sz > 0 else "short"
        hit_ai = min((abs(x - px) / px * 100 for x in ai_levels), default=99) <= 0.05
        near_px, near_d = nearest(merged, px, a.tol)
        tfl = []
        if near_px is not None:
            tags = merged[near_px]
            tfl = sorted({t.split(":")[0] for t in tags})
            kinds = sorted({t.split(":", 1)[1] for t in tags})
        else:
            kinds = []
        rec = {
            "price": round(px, 2), "size": abs(sz), "side": side,
            "unit": (tp_of.get((side, round(px, 2))) or {}).get("unit", "?"),
            "src": "结构位" if hit_ai else "补点",
            "tfs": tfl, "kinds": kinds,
            "dist_pct": round((px - mark) / mark * 100, 2),
            "tp": (tp_of.get((side, round(px, 2))) or {}).get("tp_target"),
            "smc_px": near_px,
        }
        rows_out.append(rec)

    rows_out.sort(key=lambda r: -r["price"])

    # ── 控制台表 ────────────────────────────────────────────────
    print(f"\nmark={mark}  档位 {len(rows_out)} 条\n")
    print(f"{'价格':>9} {'侧':<5}{'unit':<7}{'张':>4} {'来源':<7}{'命中周期':<16}{'SMC标签':<28}{'TP':>9} {'距现价':>8}")
    for r in rows_out:
        print(f"{r['price']:>9} {r['side']:<5}{r['unit']:<7}{r['size']:>4.0f} "
              f"{r['src']:<7}{','.join(r['tfs']) or '-':<16}"
              f"{','.join(r['kinds'])[:26]:<28}"
              f"{(('%.2f' % r['tp']) if r['tp'] else '-'):>9} {r['dist_pct']:>7.2f}%")

    n_struct = sum(1 for r in rows_out if r["src"] == "结构位")
    print(f"\n结构位 {n_struct} / 补点 {len(rows_out) - n_struct}；"
          f"AI 给了 {len(ai_levels)} 个 structure_levels")

    # ── HTML ────────────────────────────────────────────────────
    payload = {
        "mark": mark, "symbol": a.symbol, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rows": rows_out,
        "sls": [{"trigger": s["trigger"], "size": s["size"], "rule": s["rule"]} for s in sls],
        "ai_levels": sorted(ai_levels),
        "tf_stat": {tf: len(v) for tf, v in per_tf.items()},
        "plan_meta": {k: (plan.get("meta") or {}).get(k)
                      for k in ("two_way", "ladder_step_pct", "plan_reasoning")},
        "tpl": {"grid_levels": 10, "martin_slots": {"1": 3, "5": 4, "10": 6},
                "min_step_pct": 0.66, "max_step_pct": 2.2},
    }
    html = HTML_TMPL.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    out = os.path.join(ROOT, a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write(html)
    print(f"\n写出 {out}")
    return 0


HTML_TMPL = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ETH 双向阶梯挂单地图</title>
<style>
:root{--bg:#0f1216;--panel:#171c23;--line:#2a3240;--txt:#e6ebf2;--dim:#8b97a8;
--long:#3b82f6;--short:#ef4444;--struct:#22c55e;--fill:#f59e0b;--martin:#a855f7;--base:#06b6d4}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px}
h1{font-size:19px;margin:0 0 4px;font-weight:600}
.sub{color:var(--dim);font-size:12.5px;margin-bottom:18px}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:10px 14px;min-width:132px}
.card .k{color:var(--dim);font-size:11.5px}
.card .v{font-size:17px;font-weight:600;margin-top:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:16px}
.panel h2{font-size:13.5px;margin:0 0 12px;color:var(--dim);font-weight:600;
letter-spacing:.04em;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:500;padding:6px 8px;
border-bottom:1px solid var(--line);font-size:11.5px}
td{padding:6px 8px;border-bottom:1px solid rgba(42,50,64,.5);
font-variant-numeric:tabular-nums}
tr:hover td{background:rgba(59,130,246,.06)}
.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;
border:1px solid currentColor;margin-right:4px}
.t-long{color:var(--long)}.t-short{color:var(--short)}
.t-s{color:var(--struct)}.t-f{color:var(--fill)}
.t-m{color:var(--martin)}.t-g{color:var(--dim)}.t-b{color:var(--base)}
#chart{width:100%;height:640px;display:block}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--dim);font-size:12px;
margin-top:10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;
margin-right:5px;vertical-align:middle}
.foot{color:var(--dim);font-size:11.5px;line-height:1.7}
code{background:#0b0e12;padding:1px 5px;border-radius:3px;color:#9fd0ff;
font-size:11.5px}
</style></head><body><div class="wrap">
<h1>ETH_USDT 双向阶梯 · 挂单地图</h1>
<div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<div class="panel"><h2>价格轴（实盘挂单 + 止损）</h2>
<canvas id="chart"></canvas>
<div class="legend">
<span><i style="background:#3b82f6"></i>多头挂单</span>
<span><i style="background:#ef4444"></i>空头挂单</span>
<span><i style="background:#22c55e"></i>结构位命中</span>
<span><i style="background:#f59e0b"></i>价距补点</span>
<span><i style="background:#a855f7"></i>马丁位</span>
<span><i style="background:#8b97a8"></i>共用止损</span>
</div></div>
<div class="panel"><h2>逐档明细</h2><table id="tbl">
<thead><tr><th>价格</th><th>侧</th><th>unit</th><th>张数</th><th>来源</th>
<th>命中周期</th><th>SMC 标签</th><th>SMC 价</th><th>目标止盈</th><th>距现价</th></tr></thead>
<tbody></tbody></table></div>
<div class="panel"><h2>SMC 结构位覆盖（各周期贡献）</h2><div id="tfs"></div>
<div class="foot" id="foot"></div></div>
</div>
<script>
const D = __DATA__;
const rows = D.rows, mark = D.mark;
document.getElementById('sub').textContent =
  `${D.symbol} · mark ${mark} · ${D.ts} 生成 · 共 ${rows.length} 条挂单`;
const nS = rows.filter(r=>r.src==='结构位').length;
const lots = rows.reduce((a,r)=>a+r.size,0);
document.getElementById('cards').innerHTML = [
  ['现价 mark', mark.toFixed(2)],
  ['挂单档数', rows.length],
  ['总张数', lots.toFixed(0)],
  ['结构位 / 补点', `${nS} / ${rows.length-nS}`],
  ['结构位占比', ((nS/rows.length)*100).toFixed(0)+'%'],
  ['AI 给的位', D.ai_levels.length],
].map(([k,v])=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

const tbody = document.querySelector('#tbl tbody');
tbody.innerHTML = rows.map(r=>{
  const u = r.unit==='martin'?'t-m':r.unit==='base'?'t-b':'t-g';
  const s = r.src==='结构位'?'t-s':'t-f';
  return `<tr>
  <td><b>${r.price.toFixed(2)}</b></td>
  <td class="${r.side==='long'?'t-long':'t-short'}">${r.side==='long'?'多':'空'}</td>
  <td><span class="tag ${u}">${r.unit}</span></td>
  <td>${r.size}</td>
  <td><span class="tag ${s}">${r.src}</span></td>
  <td>${r.tfs.length?r.tfs.map(t=>`<span class="tag t-g">${t}</span>`).join(''):'—'}</td>
  <td style="color:var(--dim);font-size:11.5px">${r.kinds.join(', ')||'—'}</td>
  <td style="color:#7f8ea3;font-size:11.5px">${r.smc_px?r.smc_px.toFixed(1):'—'}</td>
  <td>${r.tp?r.tp.toFixed(2):'—'}</td>
  <td style="color:${r.dist_pct>=0?'#ef4444':'#22c55e'}">${r.dist_pct>0?'+':''}${r.dist_pct.toFixed(2)}%</td>
  </tr>`;
}).join('');

document.getElementById('tfs').innerHTML = '<table><thead><tr><th>周期</th><th>K线根数</th><th>本轮 SMC 价位数</th></tr></thead><tbody>'
 + Object.entries(D.tf_stat).map(([tf,n])=>{
     const cnt = rows.filter(r=>r.tfs.includes(tf)).length;
     return `<tr><td>${tf}</td><td>${({ '15m':300,'1h':240,'4h':180,'1d':120 })[tf]||''}</td>
     <td>${n}</td></tr>`;}).join('') + '</tbody></table>';
const tw = D.plan_meta.two_way||{};
document.getElementById('foot').innerHTML =
 `<br>AI 本轮：long ${(tw.long||{}).levels} 层/step ${(tw.long||{}).step_pct}% · `
 + `short ${(tw.short||{}).levels} 层/step ${(tw.short||{}).step_pct}% · `
 + `ladder_step_pct ${D.plan_meta.ladder_step_pct}<br>`
 + `补点间距 = clamp(max(min_step_pct ${D.tpl.min_step_pct}%, 当轮动态档距), …, max_step_pct ${D.tpl.max_step_pct}%)<br>`
 + `推理：${(D.plan_meta.plan_reasoning||'').slice(0,160)}`;

// ── 价格轴 ──
const cv = document.getElementById('chart'), ctx = cv.getContext('2d');
function niceStep(raw){const p=Math.pow(10,Math.floor(Math.log10(raw)));
  const n=raw/p; return (n<1.5?1:n<3?2:n<7?5:10)*p;}
function draw(){
  const dpr = window.devicePixelRatio||1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w*dpr; cv.height = h*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);

  // 视窗只框住「挂单 + 现价」，SL 在窗外时用边缘条提示
  const px = rows.map(r=>r.price).concat([mark]);
  const lo0 = Math.min(...px), hi0 = Math.max(...px);
  const pad = Math.max((hi0-lo0)*0.12, 6);
  const top = hi0+pad, bot = lo0-pad;
  const y = p => (top-p)/(top-bot)*(h-46)+30;
  const cx = w*0.5;
  const maxSize = Math.max(...rows.map(r=>r.size));

  // 横向网格 + 价格刻度
  ctx.font='11px ui-monospace,Menlo,monospace';
  const step = niceStep((top-bot)/7);
  for(let v=Math.ceil(bot/step)*step; v<=top; v+=step){
    const yy=y(v);
    ctx.strokeStyle='#1e2531'; ctx.beginPath();
    ctx.moveTo(56,yy); ctx.lineTo(w-56,yy); ctx.stroke();
    ctx.fillStyle='#5d6a7d'; ctx.textAlign='center';
    ctx.fillText(v.toFixed(0), 28, yy+4);
  }
  ctx.textAlign='left';

  // 中轴（分侧线）
  ctx.strokeStyle='#2a3240'; ctx.beginPath();
  ctx.moveTo(cx,14); ctx.lineTo(cx,h-14); ctx.stroke();

  // 现价
  ctx.strokeStyle='#e6ebf2'; ctx.setLineDash([6,4]); ctx.lineWidth=1.2;
  ctx.beginPath();ctx.moveTo(56,y(mark));ctx.lineTo(w-56,y(mark));ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle='#e6ebf2'; ctx.font='600 12px ui-monospace,Menlo,monospace';
  ctx.textAlign='right';
  ctx.fillText('mark '+mark.toFixed(2), w-60, y(mark)-7);
  ctx.textAlign='left';

  // 挂单：条宽 ∝ 张数
  const baseW = 46, maxExtra = 74;
  rows.forEach(r=>{
    const yy=y(r.price), isL = r.side==='long';
    const wBar = baseW + maxExtra*(r.size/maxSize);
    const x = isL ? cx-6-wBar : cx+6;
    const color = r.unit==='martin' ? '#a855f7'
                : r.unit==='base' ? '#06b6d4'
                : (isL ? '#3b82f6' : '#ef4444');
    ctx.globalAlpha=.30; ctx.fillStyle=color;
    ctx.fillRect(x, yy-6, wBar, 12);
    ctx.globalAlpha=1; ctx.strokeStyle=color; ctx.lineWidth=1;
    ctx.strokeRect(x, yy-6, wBar, 12);

    // 来源标记：结构位=实心圆(绿)，补点=空心方块(橙)
    const mx = cx + (isL ? -1 : 1)*3;
    if(r.src==='结构位'){
      ctx.beginPath(); ctx.arc(mx, yy, 3.6, 0, 7);
      ctx.fillStyle='#22c55e'; ctx.fill();
    }else{
      ctx.fillStyle='#f59e0b'; ctx.fillRect(mx-3, yy-3, 6, 6);
    }
    // 文字：价格·张数（外侧）
    ctx.fillStyle='#dbe3ee'; ctx.font='600 11px ui-monospace,Menlo,monospace';
    ctx.textAlign = isL?'right':'left';
    ctx.fillText(`${r.price.toFixed(1)} ×${r.size}`, isL?x-7:x+wBar+7, yy+4);
    ctx.textAlign='left';
  });

  // SL：窗内画线，窗外画边缘提示条
  D.sls.forEach(s=>{
    const above = s.trigger > top, below = s.trigger < bot;
    const yy = above ? 8 : below ? h-8 : y(s.trigger);
    const label = `SL ${s.trigger.toFixed(2)} · ${s.size>0?'+':''}${s.size}张`
                + (above?'  ↑ 视窗外':below?'  ↓ 视窗外':'');
    if(!above && !below){
      ctx.strokeStyle='#8b97a8'; ctx.setLineDash([4,3]); ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(56,yy);ctx.lineTo(w-56,yy);ctx.stroke();
      ctx.setLineDash([]);
    }else{
      ctx.fillStyle='#232b38'; ctx.fillRect(56, yy-11, w-112, 22);
    }
    ctx.fillStyle='#8b97a8'; ctx.font='11px ui-monospace,Menlo,monospace';
    ctx.textAlign='center'; ctx.fillText(label, cx, yy+4); ctx.textAlign='left';
  });
}
draw(); window.addEventListener('resize', draw);
</script></body></html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
