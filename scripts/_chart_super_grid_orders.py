# -*- coding: utf-8 -*-
"""超级网格挂单效果图：K 线 + AI 档位（martin_slots 张数）+ SL + 4H ST + 布林带。

输入：_probe/super_grid_out/raw_response.json（第七轮真实 LLM 输出）
      /tmp/eth15m.json（Gate 15m，300 根，attach 指标）
输出：_charts/super_grid_orders.html（自包含 canvas）
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from omnialpha.strategist.indicators import attach_indicators  # noqa: E402

d = json.load(open(os.path.join(ROOT, "_probe", "super_grid_out", "raw_response.json"),
                   encoding="utf-8"))
_c = d["content"].strip()
import re as _re
_m = _re.match(r"```(?:json)?\s*(\{.*\})\s*```", _c, _re.S)
plan = json.loads(_m.group(1) if _m else _c)
meta = plan.get("meta") or {}
for c in plan.get("chips") or []:
    if isinstance(c, dict) and isinstance(c.get("meta"), dict) and c["meta"].get("structure_levels"):
        meta = c["meta"]
        break

lv = meta.get("structure_levels") or []
tw = meta.get("two_way") or {}
sl = meta.get("sl")
side = "short" if (tw.get("short") or {}).get("weight", 0) > 0 else "long"
LOTS = {1: 3, 5: 4, 10: 6}

# K 线 + 指标（15m 最近 220 根）
kbars = json.load(open("/tmp/eth15m.json"))[-220:]
rows = [{"t": int(x["t"]), "o": float(x["o"]), "h": float(x["h"]),
         "l": float(x["l"]), "c": float(x["c"]), "v": float(x["v"])} for x in kbars]
attach_indicators(rows, ["boll20", "boll_upper", "boll_lower",
                         "supertrend10", "supertrend10_dir"])
mark = rows[-1]["c"]
# 4H ST 需要单独算（15m 行上的 ST 不是 4H 的）—— 拉最新 4h 并 attach
try:
    os.environ.setdefault("no_proxy", "api.gateio.ws")
    import urllib.request as _ur
    _d4 = json.load(_ur.urlopen(
        "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
        "?contract=ETH_USDT&interval=4h&limit=180", timeout=30))
    _r4 = [{"t": int(x["t"]), "o": float(x["o"]), "h": float(x["h"]),
            "l": float(x["l"]), "c": float(x["c"]), "v": float(x["v"])} for x in _d4]
    attach_indicators(_r4, ["supertrend10", "supertrend10_dir"])
    st_now = next((r["supertrend10"] for r in reversed(_r4)
                   if r.get("supertrend10") is not None), None)
    st_dir4 = next((r["supertrend10_dir"] for r in reversed(_r4)
                    if r.get("supertrend10_dir") is not None), None)
except Exception as _e:
    print("4h fetch fail:", _e)
    st_now, st_dir4 = None, None

# 档位表（按方向排）
if side == "short":
    levels = sorted([p for p in lv if p > mark])[:10]
else:
    levels = sorted([p for p in lv if p < mark], reverse=True)[:10]

bars = [{"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
         "bu": r.get("boll_upper"), "bl": r.get("boll_lower"), "bm": r.get("boll20")}
        for r in rows]
data = json.dumps({
    "bars": bars, "levels": levels, "side": side, "sl": sl, "mark": mark,
    "st4": st_now, "st4_dir": st_dir4, "lots": LOTS,
}, ensure_ascii=False)

html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>超级网格 · 挂单效果图（AI 第七轮输出）</title>
<style>
  :root{--bg:#f7f8fa;--panel:#fff;--ink:#1f2430;--muted:#6b7280;--line:#e5e7eb;
        --up:#26a69a;--down:#ef5350;--martin:#e0803a;--grid:#5b8def}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:13px/1.5 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1280px;margin:0 auto;padding:18px}
  h1{font-size:16px;margin:0 0 2px;font-weight:650}
  .sub{color:var(--muted);font-size:12px;margin-bottom:12px}
  table{border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
        border-radius:8px;overflow:hidden;font-size:12px;margin-bottom:12px}
  th,td{border-bottom:1px solid var(--line);padding:5px 12px;text-align:right;
        font-variant-numeric:tabular-nums}
  th{background:#fafbfc;color:var(--muted);font-weight:600}
  td:first-child,th:first-child{text-align:left}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
         padding:8px 4px 0;position:relative}
  canvas{display:block;width:100%;height:auto}
  .tips{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px;
        padding:9px 12px 11px;border-top:1px solid var(--line);margin-top:6px}
  .sw{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
</style>
</head>
<body>
<div class="wrap">
  <h1>超级网格 · ETH_USDT 15m · AI 档位挂单效果图</h1>
  <div class="sub">第七轮真实 LLM 输出（deepseek-v4.1-flash，__MARK__ 时点）。马丁 = 第 1/5/10 层 3/4/6 张（引擎 martin_slots），网格 = 其余层 1 张。</div>
  <div id="tbl"></div>
  <div class="panel">
    <canvas id="cv"></canvas>
    <div class="tips">
      <span><i class="sw" style="background:#e0803a"></i>马丁档（3/4/6 张）</span>
      <span><i class="sw" style="background:#5b8def"></i>网格档（1 张）</span>
      <span><i class="sw" style="background:#d33"></i>SL（AI 结构失效位）</span>
      <span><i class="sw" style="background:#9b59b6"></i>4H SuperTrend 线</span>
      <span><i class="sw" style="background:#c9ced6"></i>布林带 20</span>
    </div>
  </div>
</div>
<script>
const D = __DATA__;
const cv = document.getElementById('cv');
const PAD = {l: 8, r: 150, t: 12, b: 26};
const BJ = ts => { const d = new Date((ts + 8*3600) * 1000), p = n => String(n).padStart(2,'0');
  return {md: p(d.getUTCMonth()+1)+'-'+p(d.getUTCDate()), hm: p(d.getUTCHours())+':'+p(d.getUTCMinutes())}; };
const fmt = v => v == null ? '—' : v.toFixed(2);

function draw(){
  const dpr = window.devicePixelRatio || 1;
  const cssW = cv.parentElement.clientWidth - 8;
  const cssH = Math.max(520, Math.round(cssW * 0.52));
  cv.width = cssW * dpr; cv.height = cssH * dpr;
  cv.style.width = cssW + 'px'; cv.style.height = cssH + 'px';
  const g = cv.getContext('2d');
  g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,cssW,cssH);

  const L = PAD.l, R = cssW - PAD.r, T = PAD.t, B = cssH - PAD.b;
  const W = R - L, H = B - T, bw = W / D.bars.length;
  const X = i => L + (i + 0.5) * bw;

  let lo = Infinity, hi = -Infinity;
  for (const b of D.bars){ lo=Math.min(lo,b.l); hi=Math.max(hi,b.h);
    if(b.bu!=null) hi=Math.max(hi,b.bu); if(b.bl!=null) lo=Math.min(lo,b.bl); }
  for (const p of D.levels){ lo=Math.min(lo,p); hi=Math.max(hi,p); }
  if (D.sl!=null) hi=Math.max(hi,D.sl);
  if (D.st4!=null){ hi=Math.max(hi,D.st4); }
  const pad=(hi-lo)*0.06; lo-=pad; hi+=pad;
  const Y = v => B - (v-lo)/(hi-lo)*H;

  g.font='11px ui-monospace,Menlo,Consolas,monospace'; g.textBaseline='middle';

  // 网格 + 价格轴
  const steps=8;
  for(let k=0;k<=steps;k++){
    const v=lo+(hi-lo)*k/steps, y=Y(v);
    g.strokeStyle='#eef0f3'; g.beginPath(); g.moveTo(L,y+.5); g.lineTo(R,y+.5); g.stroke();
    g.fillStyle='#8b919c'; g.textAlign='left'; g.fillText(v.toFixed(0),R+6,y);
  }
  // 时间轴
  g.textAlign='center'; let lastH=-1,lastDay=-1;
  for(let i=0;i<D.bars.length;i++){
    const t=BJ(D.bars[i].t);
    if(t.h%4===0&&t.m===0&&t.h!==lastH){ lastH=t.h;
      g.strokeStyle='#f4f5f7'; g.beginPath(); g.moveTo(X(i)+.5,T); g.lineTo(X(i)+.5,B); g.stroke();
      g.fillStyle='#8b919c'; g.fillText(t.hm,X(i),B+13); }
    if(t.d!==lastDay&&i>0){ lastDay=t.d; g.fillStyle='#6b7280'; g.fillText(t.md,X(i),B+25); }
  }

  // 布林带
  g.lineWidth=1; g.strokeStyle='rgba(160,168,180,.7)';
  for(const key of ['bu','bl','bm']){
    g.beginPath(); let st=false;
    for(let i=0;i<D.bars.length;i++){ const v=D.bars[i][key];
      if(v==null){st=false;continue;}
      if(!st){g.moveTo(X(i),Y(v));st=true;} else g.lineTo(X(i),Y(v)); }
    g.stroke();
  }

  // 档位线（横线从出现位置延伸到图右）
  const n=D.bars.length;
  const drawLevel=(p,color,label,bold)=>{
    const y=Y(p);
    g.strokeStyle=color; g.lineWidth=bold?2.2:1.4;
    g.setLineDash(bold?[]:[6,4]);
    g.beginPath(); g.moveTo(L,y+.5); g.lineTo(R,y+.5); g.stroke();
    g.setLineDash([]);
    g.fillStyle=color; g.fillRect(R+1,y-9,PAD.r-6,18);
    g.fillStyle='#fff'; g.textAlign='left'; g.fillText(label,R+6,y);
  };
  // 类型/张数按层号
  D.levels.forEach((p,idx)=>{
    const li=idx+1;
    const isM=[1,5,10].includes(li);
    const lot=D.lots[li]||1;
    drawLevel(p, isM?'#e0803a':'#5b8def',
      `L${li} ${isM?'马丁':'网格'} ${lot}张`, isM);
  });
  // SL
  if(D.sl!=null){
    const y=Y(D.sl);
    g.strokeStyle='#d33'; g.lineWidth=2; g.setLineDash([8,5]);
    g.beginPath(); g.moveTo(L,y+.5); g.lineTo(R,y+.5); g.stroke(); g.setLineDash([]);
    g.fillStyle='#d33'; g.fillRect(R+1,y-9,PAD.r-6,18);
    g.fillStyle='#fff'; g.textAlign='left'; g.fillText('SL '+fmt(D.sl),R+6,y);
  }
  // 4H ST 线
  if(D.st4!=null){
    const y=Y(D.st4);
    g.strokeStyle='#9b59b6'; g.lineWidth=2; g.setLineDash([10,4,3,4]);
    g.beginPath(); g.moveTo(L,y+.5); g.lineTo(R,y+.5); g.stroke(); g.setLineDash([]);
    g.fillStyle='#9b59b6'; g.textAlign='left';
    const dirTxt = D.st4_dir === -1 ? '涨(线=支撑)' : (D.st4_dir === 1 ? '跌(线=压力)' : '');
    g.fillText('4H ST '+fmt(D.st4)+' '+dirTxt, L+6, y-10);
  }

  // K 线
  const cw=Math.max(1,Math.min(9,bw*0.62));
  for(let i=0;i<D.bars.length;i++){
    const b=D.bars[i], up=b.c>=b.o;
    g.strokeStyle=up?'#26a69a':'#ef5350'; g.fillStyle=up?'#26a69a':'#ef5350';
    g.beginPath(); g.moveTo(Math.round(X(i))+.5,Y(b.h)); g.lineTo(Math.round(X(i))+.5,Y(b.l)); g.stroke();
    const top=Y(Math.max(b.o,b.c)), hgt=Math.max(1,Y(Math.min(b.o,b.c))-top);
    g.fillRect(X(i)-cw/2,top,cw,hgt);
    if(bw>3.4) g.strokeRect(Math.round(X(i)-cw/2)+.5,Math.round(top)+.5,Math.round(cw)-1,Math.round(hgt)-1);
  }

  // 现价标签
  const y=Y(D.mark);
  g.fillStyle='#20242d'; g.fillRect(R+1,y-9,PAD.r-6,18);
  g.fillStyle='#fff'; g.textAlign='left'; g.fillText('现价 '+fmt(D.mark),R+6,y);
}

function init(){
  const rows = D.levels.map((p,i)=>{
    const li=i+1, isM=[1,5,10].includes(li), lot=D.lots[li]||1;
    return `<tr><td>L${li}</td><td style="color:${isM?'#e0803a':'#5b8def'};font-weight:600">${isM?'马丁':'网格'}</td><td>${lot} 张</td><td><b>${fmt(p)}</b></td><td>${fmt(Math.abs(p-D.mark))} 点</td></tr>`;
  }).join('');
  document.getElementById('tbl').innerHTML =
    `<table><tr><th>层</th><th>类型</th><th>张数</th><th>挂单价</th><th>距现价</th></tr>${rows}`+
    `<tr><td>SL</td><td style="color:#d33;font-weight:600">止损</td><td>—</td><td style="color:#d33"><b>${fmt(D.sl)}</b></td><td>${fmt(Math.abs(D.sl-D.mark))} 点</td></tr></table>`;
  draw();
}
window.addEventListener('resize',draw);
init();
</script>
</body>
</html>
"""
html = html.replace("__DATA__", data).replace("__MARK__", f"{mark}")
out = os.path.join(ROOT, "_charts", "super_grid_orders.html")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print("wrote", out, "| side:", side, "| levels:", levels, "| sl:", sl, "| mark:", mark)
