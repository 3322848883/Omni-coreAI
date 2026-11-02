#!/usr/bin/env python3
"""真实行情一轮的 K 线 + 阶梯挂单图（自包含 HTML canvas，无 CDN）。

用法：
    python scripts/_chart_ladder_kline.py --plan <inbox 的 plan json> [--candles 300]

断轴设计（2026-11-03）：K 线只在 2210~2320 一小段，档位却铺到 2098~2466 ——
单一线性轴会把蜡烛压成纸片。分三段映射：上延伸段(空侧深档) / K 线段 / 下延伸段(多侧深档)，
段间画 ≈ 断轴符；SL 在两端的极端(1798/2766)用边缘提示条。
时间轴日期标签用**日期字符串比较**判换日（2026-10-29 踩过 `t.h===0` 的坑）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fetch_klines(tf: str = "15m", limit: int = 300) -> list[dict]:
    u = (f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
         f"?contract=ETH_USDT&interval={tf}&limit={limit}")
    req = urllib.request.Request(u, headers={"Accept": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=25).read())
    d.sort(key=lambda x: x["t"])
    return [{"t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
             "l": float(k["l"]), "c": float(k["c"])} for k in d]


def load_data(plan_path: Path) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    meta = plan.get("meta") or {}
    db = sqlite3.connect(ROOT / "data/accounts/gate-grid-paper/account.db")
    try:
        rows = db.execute(
            "select price,size from orders where status='open' order by price desc").fetchall()
        sls = db.execute(
            "select trigger_price,size,rule from price_orders where status='untriggered'").fetchall()
    finally:
        db.close()
    levels = []
    lv_path = ROOT / "data/bots/martin-grid-bi-gp/state/ladder_levels.json"
    if lv_path.exists():
        levels = json.loads(lv_path.read_text(encoding="utf-8"))["levels"]
    return {
        "plan": {
            "cycle_id": plan_path.stem,  # 顶层无 cycle 键，用文件名
            "structure_levels": meta.get("structure_levels") or [],
            "two_way": meta.get("two_way") or {},
            "reasoning": (meta.get("plan_reasoning") or plan.get("reasoning") or "")[:200],
        },
        "longs": [[p, s] for p, s in rows if s > 0],
        "shorts": [[p, s] for p, s in rows if s < 0],
        "sls": [[t, s, r] for t, s, r in sls],
        "units": {f"{x['price']}|{x['side']}": x["unit"] for x in levels},
        "tps": {f"{x['price']}|{x['side']}": x.get("tp_target") for x in levels},
    }


HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>ETH 15m · 结构位阶梯挂单</title>
<style>
  html,body{margin:0;background:#0d1117;color:#dbe3ee;font:13px/1.5 "Segoe UI",system-ui,sans-serif}
  .wrap{padding:14px 18px}
  h1{font-size:15px;margin:0 0 2px;color:#e6ebf2}
  .sub{color:#7f8ea3;font-size:12px;margin-bottom:10px}
  #chart{width:100%;height:860px;display:block;background:#0d1117;border:1px solid #1e2531;border-radius:8px}
  #tip{position:fixed;pointer-events:none;background:#161d29;border:1px solid #2a3240;border-radius:6px;
       padding:7px 10px;font:12px ui-monospace,monospace;display:none;z-index:9;white-space:pre;line-height:1.55}
  .legend{display:flex;gap:18px;flex-wrap:wrap;margin:8px 2px;color:#8b97a8;font-size:12px}
  .legend i{display:inline-block;width:18px;height:0;border-top:3px solid;margin-right:5px;vertical-align:middle}
  .lv{color:#22c55e}.ls{color:#ef4444}.lm{color:#f59e0b}.st{color:#22d3ee}.slc{color:#8b97a8}
</style></head><body><div class="wrap">
<h1>ETH_USDT 永续 · 15m K线 + 双向阶梯挂单（真实行情一轮）</h1>
<div class="sub" id="meta"></div>
<div class="legend">
  <span><i class="lv"></i>多侧网格档</span><span><i class="ls"></i>空侧网格档</span>
  <span><i class="lm"></i>马丁档（两翼同色，粗线）</span><span><i class="st"></i>AI 结构位</span>
  <span><i class="slc"></i>SL（红虚）· mark（灰点划）</span>
  <span>断轴 ≈ = 中间省略无波动区</span>
</div>
<canvas id="chart"></canvas>
<div id="tip"></div>
</div>
<script>
const D = __DATA__;
const cv = document.getElementById('chart'), ctx = cv.getContext('2d');
const tip = document.getElementById('tip');
document.getElementById('meta').textContent =
  'cycle ' + D.plan.cycle_id + ' · AI: ' + JSON.stringify(D.plan.two_way) +
  ' · 结构位 ' + D.plan.structure_levels.length + ' 个 · “' + D.plan.reasoning + '…”';

// ── 断轴三段 ─────────────────────────────────────────────
const KL_HI = Math.max(...D.k.map(r=>r.h)), KL_LO = Math.min(...D.k.map(r=>r.l));
const ALL = D.longs.map(r=>r[0]).concat(D.shorts.map(r=>r[0]));
const L_HI = Math.max(...ALL), L_LO = Math.min(...ALL);
const upTop = Math.ceil(Math.max(L_HI, KL_HI) + 4);      // 上延伸段顶
const upBot = Math.floor(KL_HI + 6);                      // 上延伸段底（略高于 K 线顶）
const dnTop = Math.ceil(KL_LO - 6);                       // 下延伸段顶
const dnBot = Math.floor(Math.min(L_LO, KL_LO) - 4);      // 下延伸段底
let SEGS = [];
function layout(H){
  SEGS = [
    {p1:upBot, p2:upTop, y1:0, y2:0},      // 占位，下面按比例填
  ];
  // 比例：K 线段 56%，上延伸 18%，下延伸 18%，断轴带 2×~14px，边缘 SL 条各 22px
  const bar = 24, gap = 16;
  const avail = H - 2*bar - 2*gap - 30;    // 30 给时间轴
  const hUp = avail*0.19, hK = avail*0.56, hDn = avail*0.19;
  let y = bar;
  SEGS = [
    {p1:upTop, p2:upBot, y0:y, y1:y+hUp},
    {p1:KL_HI, p2:KL_LO, y0:y+hUp+gap, y1:y+hUp+gap+hK},
    {p1:dnTop, p2:dnBot, y0:y+hUp+gap+hK+gap, y1:y+hUp+gap+hK+gap+hDn},
  ];
  return y+hUp+gap+hK+gap+hDn+bar+30;     // bottom
}
function yOf(p){
  for(const s of SEGS){
    if(p<=s.p1 && p>=s.p2) return s.y0 + (s.p1-p)/(s.p1-s.p2)*(s.y1-s.y0);
  }
  return p>SEGS[0].p1 ? 2 : SEGS[2].y1+2;
}
function draw(){
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W*dpr; cv.height = H*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  const bottom = layout(H);
  const padL = 64, padR = 96;
  const n = D.k.length, cw = (W-padL-padR)/n;
  const xOf = i => padL + i*cw + cw/2;

  // 分段背景 + 断轴符 + 价格刻度
  ctx.font='11px ui-monospace,Menlo,monospace';
  SEGS.forEach((s,si)=>{
    if(si>0){ // 断轴带
      const gy = s.y0-8;
      ctx.strokeStyle='#232b38'; ctx.fillStyle='#0d1117';
      ctx.beginPath(); ctx.rect(padL-8,gy-8,W-padL-padR+16,16); ctx.fill(); ctx.stroke();
      ctx.fillStyle='#5d6a7d'; ctx.textAlign='center';
      ctx.fillText('≈ ≈ ≈  断轴（此区间无 K 线，仅延伸档位） ≈ ≈ ≈', W/2, gy+4);
    }
    // 该段的价格刻度
    const span = s.p1-s.p2;
    const step = niceStep(span/6);
    for(let v=Math.ceil(s.p2/step)*step; v<=s.p1; v+=step){
      const yy = yOf(v);
      ctx.strokeStyle='#1a212c'; ctx.beginPath();
      ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
      ctx.fillStyle='#5d6a7d'; ctx.textAlign='right';
      ctx.fillText(v.toFixed(0), padL-8, yy+4);
    }
  });

  // K 线（中国惯例：涨红跌绿）
  D.k.forEach((r,i)=>{
    const x = xOf(i), up = r.c>=r.o;
    const col = up ? '#e05252' : '#3fa66b';
    ctx.strokeStyle=col; ctx.fillStyle=col; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x, yOf(r.h)); ctx.lineTo(x, yOf(r.l)); ctx.stroke();
    const bw = Math.max(cw*0.62, 1.4);
    const yO=yOf(r.o), yC=yOf(r.c);
    ctx.fillRect(x-bw/2, Math.min(yO,yC), bw, Math.max(Math.abs(yC-yO),1));
  });

  // 时间轴（只标 K 线段下沿；换日 = 日期字符串变化）
  const kb = SEGS[1].y1;
  ctx.textAlign='center';
  let lastDay='';
  D.k.forEach((r,i)=>{
    const dt = new Date(r.t*1000);
    const day = dt.toISOString().slice(0,10);
    if(day!==lastDay){
      lastDay=day;
      ctx.fillStyle='#7f8ea3';
      ctx.fillText(day.slice(5), xOf(i), kb+16);
      ctx.strokeStyle='#242d3b'; ctx.beginPath();
      ctx.moveTo(xOf(i), SEGS[1].y0); ctx.lineTo(xOf(i), kb); ctx.stroke();
    }
  });

  // mark 价
  ctx.strokeStyle='#8b97a8'; ctx.setLineDash([2,4]); ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(padL,yOf(D.mark)); ctx.lineTo(W-padR,yOf(D.mark)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle='#c9d3e0'; ctx.font='600 11px ui-monospace,monospace';
  ctx.textAlign='left'; ctx.fillText('mark '+D.mark.toFixed(2), W-padR+6, yOf(D.mark)+4);

  // 档位横线：马丁橙粗、网格按侧色；结构位加青色三角
  function line(p, color, w, dash){
    ctx.strokeStyle=color; ctx.lineWidth=w; if(dash)ctx.setLineDash(dash);
    ctx.beginPath(); ctx.moveTo(padL,yOf(p)); ctx.lineTo(W-padR,yOf(p)); ctx.stroke();
    ctx.setLineDash([]);
  }
  const unitOf = (p,side)=> D.units[p.toFixed(2)+'|'+side] || D.units[p+'|'+side] || 'grid';
  D.longs.forEach(([p,s])=>{ const u=unitOf(p,'long');
    line(p, u==='martin'?'#f59e0b':'#22c55e', u==='martin'?3:1.5); });
  D.shorts.forEach(([p,s])=>{ const u=unitOf(p,'short');
    line(p, u==='martin'?'#f59e0b':'#ef4444', u==='martin'?3:1.5); });

  // AI 结构位标记（青三角，画在价格轴左缘）
  ctx.fillStyle='#22d3ee';
  D.plan.structure_levels.forEach(p=>{
    const yy=yOf(p);
    ctx.beginPath(); ctx.moveTo(padL-14,yy-4); ctx.lineTo(padL-6,yy); ctx.lineTo(padL-14,yy+4);
    ctx.closePath(); ctx.fill();
  });

  // SL：窗内画线，窗外边缘条
  D.sls.forEach(([t,s,r])=>{
    const inside = t<=upTop && t>=dnBot;
    if(inside){ line(t,'#8b97a8',1.2,[6,4]); }
    const yy = t>upTop ? 12 : (t<dnBot ? bottom-12 : yOf(t));
    const lab = `SL ${t.toFixed(1)} · ${s>0?'+':''}${s}张 · ${r===2?'跌破卖平':'涨破买平'}`
              + (inside?'':'（视窗外）');
    ctx.fillStyle='#39424f'; ctx.fillRect(W/2-170, yy-9, 340, 18);
    ctx.fillStyle='#a8b3c2'; ctx.font='11px ui-monospace,monospace'; ctx.textAlign='center';
    ctx.fillText(lab, W/2, yy+4);
  });

  // 右侧档位标签（张数 + tp），按价格从高到低画，与上一标签保持 ≥12px
  ctx.font='10.5px ui-monospace,monospace';
  const kbBottom = SEGS[1].y1;
  function tag(p, s, side, st){
    const u = unitOf(p,side), tp = D.tps[p.toFixed(2)+'|'+side] || D.tps[p+'|'+side];
    const txt = `${p.toFixed(1)} ${s>0?'+':''}${Math.round(Math.abs(s))}张 ${u==='martin'?'马丁':''}`
              + (tp?` →TP ${(+tp).toFixed(1)}`:'');
    const xx = side==='long' ? padL+6 : W-padR-6;
    ctx.textAlign = side==='long' ? 'left' : 'right';
    let yy = yOf(p) + (yOf(p) > yOf(D.mark) ? -4 : 12);
    // 底部避让时间轴：K 线段底之下 → 画线上方
    if(yy > kbBottom + 6) yy = yOf(p) - 4;
    if(st.last !== null && yy < st.last + 12) yy = st.last + 12;
    st.last = yy;
    ctx.fillStyle = u==='martin' ? '#f59e0b' : (side==='long' ? '#57c785' : '#e57373');
    ctx.fillText(txt, xx, yy);
  }
  const stL={last:null}, stS={last:null};
  [...D.longs].sort((a,b)=>b[0]-a[0]).forEach(([p,s])=>tag(p,s,'long',stL));
  [...D.shorts].sort((a,b)=>b[0]-a[0]).forEach(([p,s])=>tag(p,s,'short',stS));

  // hover 检测：K 线 OHLC / 档位线
  cv.onmousemove = ev=>{
    const rect = cv.getBoundingClientRect();
    const mx = ev.clientX-rect.left, my = ev.clientY-rect.top;
    let txt = null;
    const i = Math.round((mx-padL)/cw-0.5);
    if(i>=0 && i<n && my>=SEGS[1].y0 && my<=SEGS[1].y1){
      const r=D.k[i], dt=new Date(r.t*1000).toISOString().replace('T',' ').slice(0,16);
      txt = `${dt} (UTC)\nO ${r.o.toFixed(2)}  H ${r.h.toFixed(2)}\nL ${r.l.toFixed(2)}  C ${r.c.toFixed(2)}`;
    }
    if(!txt){
      const hit = (arr,side)=>arr.find(([p])=>Math.abs(yOf(p)-my)<4);
      const hl = hit(D.longs,'long'), hs = hit(D.shorts,'short');
      const [p,s] = hl||hs||[null];
      if(p){
        const side = hl?'long':'short', u=unitOf(p,side);
        const tp = D.tps[p.toFixed(2)+'|'+side] || D.tps[p+'|'+side];
        txt = `${side==='long'?'多':'空'}档 ${p.toFixed(2)} · ${Math.abs(s)}张 · ${u}` + (tp?`\nTP ${(+tp).toFixed(2)}`:'');
      }
    }
    if(txt){ tip.style.display='block'; tip.textContent=txt;
      tip.style.left=(ev.clientX+14)+'px'; tip.style.top=(ev.clientY+14)+'px';
    } else tip.style.display='none';
  };
  cv.onmouseleave = ()=>tip.style.display='none';
}
function niceStep(raw){const p=Math.pow(10,Math.floor(Math.log10(raw)));
  const n=raw/p; return (n<1.5?1:n<3?2:n<7?5:10)*p;}
draw(); window.addEventListener('resize', draw);
</script></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--candles", type=int, default=300)
    args = ap.parse_args()

    data = load_data(Path(args.plan))
    data["k"] = fetch_klines("15m", args.candles)
    # mark 价取最后一根收盘（近似现价）
    data["mark"] = data["k"][-1]["c"]

    out = ROOT / "_charts" / "ladder_kline.html"
    out.parent.mkdir(exist_ok=True)
    html = HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out.write_text(html, encoding="utf-8")
    print("written", out)


if __name__ == "__main__":
    main()
