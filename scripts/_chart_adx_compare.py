"""ETH 15m ADX 双算法对比图（自包含 HTML）。

ADX(Wilder/RMA)  vs  ADX(SMA, BeikabuOyaji 脚本)  +  DI+/DI-
输出：_charts/adx_compare.html
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omnialpha.strategist.indicators import adx, adx_pine  # noqa: E402

SRC = "/tmp/eth15m.json"
OUT = os.path.join(ROOT, "_charts", "adx_compare.html")
SHOW = 200

d = json.load(open(SRC))
h = [float(x["h"]) for x in d]
l = [float(x["l"]) for x in d]
c = [float(x["c"]) for x in d]
ts = [int(x["t"]) for x in d]

w = adx(h, l, c, 14)
p = adx_pine(h, l, c, 14)

bars = []
for i in range(len(c) - SHOW, len(c)):
    bars.append({
        "t": ts[i],
        "c": c[i],
        "aw": w["adx"][i],
        "ap": p["adx"][i],
        "dp": w["plus_di"][i],
        "dm": w["minus_di"][i],
    })

data_json = json.dumps(bars, ensure_ascii=False)

html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>ETH 15m · ADX 双算法对比</title>
<style>
  :root{--bg:#f7f8fa;--panel:#fff;--ink:#1f2430;--muted:#6b7280;--line:#e5e7eb}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:13px/1.5 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1240px;margin:0 auto;padding:18px}
  h1{font-size:16px;margin:0 0 2px;font-weight:650}
  .sub{color:var(--muted);font-size:12px;margin-bottom:14px}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
  .card .k{font-size:11px;color:var(--muted)}
  .card .v{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums;margin-top:2px}
  .card .n{font-size:11px;color:var(--muted);margin-top:2px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 4px 0;position:relative}
  canvas{display:block;width:100%;height:auto}
  .tips{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;
        padding:10px 12px 12px;border-top:1px solid var(--line);margin-top:6px}
  .sw{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
  #tip{position:absolute;pointer-events:none;background:rgba(32,36,45,.94);color:#fff;
       padding:7px 9px;border-radius:6px;font-size:11.5px;line-height:1.55;display:none;
       font-variant-numeric:tabular-nums;white-space:nowrap;z-index:5}
</style>
</head>
<body>
<div class="wrap">
  <h1>ETH_USDT 永续 · 15m · ADX(14) 两套算法</h1>
  <div class="sub">ADX = SMA(DX)（BeikabuOyaji「ADX and DI for v4」＝本项目默认 <code>adx14</code>） vs ADX = RMA(DX)（TV 内置 ta.dmi＝<code>adx14_wilder</code>）。DI 两版相同。北京时间。</div>

  <div class="cards">
    <div class="card"><div class="k">ADX · Wilder / RMA</div><div class="v" id="c-w">—</div><div class="n">adx14_wilder（备选）</div></div>
    <div class="card"><div class="k">ADX · SMA</div><div class="v" id="c-p" style="color:#e0803a">—</div><div class="n">adx14（默认）</div></div>
    <div class="card"><div class="k">当前差值</div><div class="v" id="c-d">—</div><div class="n" id="c-dn">—</div></div>
    <div class="card"><div class="k">DI+ / DI-</div><div class="v" id="c-di">—</div><div class="n" id="c-din">—</div></div>
  </div>

  <div class="panel">
    <canvas id="cv"></canvas>
    <div id="tip"></div>
    <div class="tips">
      <span><i class="sw" style="background:#2f4b7c"></i>ADX Wilder（RMA）</span>
      <span><i class="sw" style="background:#e0803a"></i>ADX SMA（默认，你的脚本）</span>
      <span><i class="sw" style="background:#26a69a"></i>DI+</span>
      <span><i class="sw" style="background:#ef5350"></i>DI-</span>
      <span>虚线 = 阈值 20</span>
    </div>
  </div>
</div>
<script>
const BARS = __DATA__;
const cv = document.getElementById('cv'), tip = document.getElementById('tip');
let GEO = null;
function bj(ts){const d=new Date((ts+8*3600)*1000),p=n=>String(n).padStart(2,'0');
  return {md:p(d.getUTCMonth()+1)+'-'+p(d.getUTCDate()),hm:p(d.getUTCHours())+':'+p(d.getUTCMinutes()),h:d.getUTCHours(),m:d.getUTCMinutes(),d:d.getUTCDate()};}
const f2 = v => v==null?'—':v.toFixed(2);

function draw(){
  const dpr = window.devicePixelRatio||1;
  const cssW = cv.parentElement.clientWidth-8;
  const cssH = Math.max(520, Math.round(cssW*0.62));
  cv.width=cssW*dpr; cv.height=cssH*dpr; cv.style.width=cssW+'px'; cv.style.height=cssH+'px';
  const g=cv.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,cssW,cssH);
  const L=8,R=cssW-64,T=14;
  const gap=26, h1=Math.round((cssH-T-gap-30)*0.64), h2=cssH-T-gap-30-h1;
  const t1=T, b1=T+h1, t2=b1+gap, b2=b1+gap+h2;
  const W=R-L, bw=W/BARS.length, X=i=>L+(i+0.5)*bw;

  // ADX 面板范围
  let lo=0, hi=60;
  for(const b of BARS){ if(b.aw!=null) hi=Math.max(hi,b.aw); if(b.ap!=null) hi=Math.max(hi,b.ap); }
  hi=Math.ceil(hi/10)*10;
  const Y1 = v => b1 - (v-lo)/(hi-lo)*h1;
  // DI 面板 0..60
  const dhi=60, Y2 = v => b2 - (v-0)/(dhi-0)*h2;

  g.font='11px ui-monospace,Menlo,Consolas,monospace'; g.textBaseline='middle';

  // 网格 + 轴
  for(let k=0;k<=6;k++){
    const v=lo+(hi-lo)*k/6;
    const y=Y1(v);
    g.strokeStyle='#eef0f3'; g.beginPath(); g.moveTo(L,y+.5); g.lineTo(R,y+.5); g.stroke();
    g.fillStyle='#8b919c'; g.textAlign='left'; g.fillText(v.toFixed(0),R+6,y);
  }
  for(let k=0;k<=3;k++){
    const v=k*20, y=Y2(v);
    g.strokeStyle='#eef0f3'; g.beginPath(); g.moveTo(L,y+.5); g.lineTo(R,y+.5); g.stroke();
    g.fillStyle='#8b919c'; g.textAlign='left'; g.fillText(v.toFixed(0),R+6,y);
  }
  // 阈值 20
  g.setLineDash([4,4]); g.strokeStyle='#9aa1ac'; g.lineWidth=1;
  g.beginPath(); g.moveTo(L,Y1(20)); g.lineTo(R,Y1(20)); g.stroke();
  g.beginPath(); g.moveTo(L,Y2(20)); g.lineTo(R,Y2(20)); g.stroke();
  g.setLineDash([]);

  // 时间轴
  g.textAlign='center'; let lastH=-1,lastDay=-1;
  for(let i=0;i<BARS.length;i++){
    const t=bj(BARS[i].t);
    if(t.h%4===0&&t.m===0&&t.h!==lastH){ lastH=t.h;
      g.strokeStyle='#f4f5f7'; g.beginPath(); g.moveTo(X(i)+.5,t1); g.lineTo(X(i)+.5,b2); g.stroke();
      g.fillStyle='#8b919c'; g.fillText(t.hm,X(i),b2+13);
    }
    if(t.d!==lastDay&&i>0){ lastDay=t.d; g.fillStyle='#6b7280'; g.fillText(t.md,X(i),b2+25); }
  }

  // 两版 ADX 差异填色
  g.beginPath();
  for(let i=0;i<BARS.length;i++){ const v=BARS[i].aw; if(v==null) continue; g.lineTo(X(i),Y1(v)); }
  for(let i=BARS.length-1;i>=0;i--){ const v=BARS[i].ap; if(v==null) continue; g.lineTo(X(i),Y1(v)); }
  g.closePath(); g.fillStyle='rgba(224,128,58,.12)'; g.fill();

  // 线
  function line(key,Y,color,width){
    g.strokeStyle=color; g.lineWidth=width; g.lineJoin='round'; g.beginPath();
    let started=false;
    for(let i=0;i<BARS.length;i++){ const v=BARS[i][key];
      if(v==null){started=false;continue;}
      if(!started){g.moveTo(X(i),Y(v));started=true;} else g.lineTo(X(i),Y(v)); }
    g.stroke();
  }
  line('aw',Y1,'#2f4b7c',2.2);
  line('ap',Y1,'#e0803a',2.2);
  line('dp',Y2,'#26a69a',1.8);
  line('dm',Y2,'#ef5350',1.8);

  // 右端标签
  const lb=BARS[BARS.length-1];
  function tag(v,Y,color){
    if(v==null) return; const y=Y(v);
    g.fillStyle=color; g.fillRect(R+1,y-8,60,16);
    g.fillStyle='#fff'; g.textAlign='left'; g.fillText(f2(v),R+5,y);
  }
  tag(lb.ap,Y1,'#e0803a'); tag(lb.aw,Y1,'#2f4b7c');
  tag(lb.dp,Y2,'#26a69a'); tag(lb.dm,Y2,'#ef5350');

  GEO={X,bw,n:BARS.length,Y1,Y2,L,R};
}

function hover(ev){
  if(!GEO) return;
  const r=cv.getBoundingClientRect(), mx=ev.clientX-r.left;
  const i=Math.floor((mx-GEO.L)/GEO.bw);
  if(i<0||i>=GEO.n){tip.style.display='none';return;}
  const b=BARS[i],t=bj(b.t);
  tip.innerHTML=t.md+' '+t.hm+'　收盘 '+b.c.toFixed(2)+
    '<br>ADX Wilder '+f2(b.aw)+'　SMA '+f2(b.ap)+
    '<br>差 '+(b.aw!=null&&b.ap!=null?(b.ap-b.aw).toFixed(2):'—')+
    '<br>DI+ '+f2(b.dp)+'　DI- '+f2(b.dm);
  tip.style.display='block';
  tip.style.left=Math.min(GEO.X(i)+12,cv.clientWidth-190)+'px';
  tip.style.top=Math.max(6,ev.clientY-r.top-56)+'px';
}

function init(){
  const lb=BARS[BARS.length-1];
  document.getElementById('c-w').textContent=f2(lb.aw);
  document.getElementById('c-p').textContent=f2(lb.ap);
  const diff=lb.ap-lb.aw;
  document.getElementById('c-d').textContent=(diff>=0?'+':'')+diff.toFixed(2);
  document.getElementById('c-dn').textContent=(diff/lb.aw*100).toFixed(0)+'% 偏高（SMA 版）';
  document.getElementById('c-di').textContent=f2(lb.dp)+' / '+f2(lb.dm);
  document.getElementById('c-din').textContent=lb.dp>lb.dm?'多头占优':'空头占优';
  document.getElementById('c-di').style.color=lb.dp>lb.dm?'#26a69a':'#ef5350';
  draw();
}
window.addEventListener('resize',draw);
cv.addEventListener('mousemove',hover);
cv.addEventListener('mouseleave',()=>{tip.style.display='none';});
init();
</script>
</body>
</html>
"""

html = html.replace("__DATA__", data_json)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print("wrote", OUT, len(bars), "bars")
