# -*- coding: utf-8 -*-
"""把两版回放轨迹画成 K 线对比 HTML（canvas，无外部依赖）。

用法：python scripts/_make_replay_chart.py
输出：reports/replay_kline_chart.html
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "scripts/_kline_data.json").read_text(encoding="utf-8"))
BARS = DATA["bars"]
TMP = Path("C:/Users/w6485/AppData/Local/Temp")

SEGS = [("t1", 0, 400, "测试 1 · 较早 4.2 天"), ("t2", 400, 800, "测试 2 · 最近 4.2 天")]


def load(tag, seg):
    return json.loads((TMP / f"tr_{tag}_{seg}.json").read_text(encoding="utf-8"))


def slim(tr, start, end):
    out = []
    for x in tr:
        i = x["i"]
        b = BARS[i]
        out.append({
            "i": i, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
            "s": (x["auto_side"] or "")[:1],            # l / s / ''
            "b": [p[0] for p in (x["book"] or [])],     # 挂单价
            "p": x["pos"],
            "bl": 1 if x["blocked"] else 0,
        })
    return out


panels = {}
for seg, a, b, _title in SEGS:
    for tag in ("live", "local"):
        d = load(tag, seg)
        panels[f"{seg}_{tag}"] = {
            "bars": slim(d["trace"], a, b),
            "fills": d["fills"],
            "sl": d["sl_hits"],
            "sum": d["summary"],
        }

ema_map = {}
for seg, a, b, _t in SEGS:
    for x in load("local", seg)["trace"]:
        ema_map[str(x["i"])] = x.get("ema200_4h")

payload = json.dumps({"panels": panels,
                      "segs": [[s[0], s[1], s[2], s[3]] for s in SEGS],
                      "ema": ema_map},
                     ensure_ascii=False, separators=(",", ":"))

HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>阶梯回放 · 线上 vs 本地（真实 ETH 15m）</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font:14px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif;
     padding:28px 32px 60px}
h1{font-size:21px;font-weight:600;margin-bottom:4px}
.sub{color:#8b949e;font-size:13px;margin-bottom:22px}
.seg{margin-bottom:34px}
.seg h2{font-size:16px;font-weight:600;margin-bottom:10px;padding-left:9px;
        border-left:3px solid #58a6ff}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px 10px}
.card h3{font-size:13px;font-weight:600;margin-bottom:6px;display:flex;
         justify-content:space-between;align-items:center}
.tag{font-size:11px;padding:1px 7px;border-radius:9px;font-weight:500}
.tag.live{background:#3d1f22;color:#ff7b72;border:1px solid #6b2b30}
.tag.local{background:#12261b;color:#56d364;border:1px solid #1f5c38}
canvas{width:100%;height:280px;display:block;margin-top:4px}
.stats{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:12px;color:#8b949e}
.stats b{color:#e6edf3;font-weight:600}
.stats .hl{color:#f0883e}
.legend{display:flex;gap:16px;font-size:12px;color:#8b949e;margin:12px 0 0;flex-wrap:wrap}
.legend i{display:inline-block;width:11px;height:3px;vertical-align:middle;margin-right:5px}
.note{background:#161b22;border:1px solid #30363d;border-left:3px solid #d29922;
      border-radius:6px;padding:12px 16px;font-size:12.5px;color:#a8b3bf;margin-top:8px}
.note b{color:#e6edf3}
</style></head><body>
<h1>阶梯引擎回放 · 线上版 vs 本地版</h1>
<div class="sub">真实 ETH_USDT 15m K 线（服务器 kline.db）· 每根 K 线一轮 · 两版跑同一份生产配置</div>
<div class="legend">
  <span><i style="background:#8b949e"></i>K 线</span>
  <span><i style="background:#e3b341"></i>4H EMA200</span>
  <span><i style="background:#3b82f6"></i>多头挂单/成交</span>
  <span><i style="background:#f59e0b"></i>空头挂单/成交</span>
  <span><i style="background:#f85149"></i>SL 触发</span>
  <span>底部色带 = 引擎方向（蓝=多 / 橙=空）</span>
</div>
<div id="app"></div>
<div class="note" id="note"></div>
<script>
const P = __PAYLOAD__;
const SEGS = P.segs, PANELS = P.panels;
const COL = {long:"#3b82f6", short:"#f59e0b", sl:"#f85149", k:"#8b949e", ema:"#e3b341"};

function draw(cv, p, bars){
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W*dpr; cv.height = H*dpr;
  const g = cv.getContext("2d"); g.scale(dpr,dpr); g.clearRect(0,0,W,H);
  const PADL=44, PADR=8, PADT=8, PADB=26;
  const iw = W-PADL-PADR, ih = H-PADT-PADB;
  const data = p.bars;
  let lo = Math.min(...data.map(d=>d.l)), hi = Math.max(...data.map(d=>d.h));
  const emas = data.map(d=>EMA[d.i]).filter(v=>v);
  if (emas.length){ lo=Math.min(lo,...emas); hi=Math.max(hi,...emas); }
  const pad = (hi-lo)*0.05; lo-=pad; hi+=pad;
  const X = i => PADL + (i-data[0].i)/(data.length-1)*iw;
  const Y = v => PADT + (hi-v)/(hi-lo)*ih;

  // 网格 + y 轴
  g.strokeStyle="#21262d"; g.lineWidth=1; g.font="10px system-ui"; g.fillStyle="#6e7681";
  for(let k=0;k<=4;k++){
    const y = PADT + ih*k/4, v = hi-(hi-lo)*k/4;
    g.beginPath(); g.moveTo(PADL,y); g.lineTo(W-PADR,y); g.stroke();
    g.fillText(v.toFixed(0), 6, y+3);
  }

  // 蜡烛
  const bw = Math.max(1, iw/data.length*0.62);
  data.forEach(d=>{
    const x=X(d.i), up=d.c>=d.o;
    g.strokeStyle = up? "#3fb950":"#f85149";
    g.fillStyle   = up? "#3fb950":"#f85149";
    g.beginPath(); g.moveTo(x,Y(d.h)); g.lineTo(x,Y(d.l)); g.stroke();
    const y1=Y(Math.max(d.o,d.c)), y2=Y(Math.min(d.o,d.c));
    g.fillRect(x-bw/2, y1, bw, Math.max(1,y2-y1));
  });

  // 4H EMA200
  g.strokeStyle=COL.ema; g.lineWidth=1.4; g.beginPath(); let started=false;
  data.forEach(d=>{
    const v = EMA[d.i];
    if(!v) return;
    const x=X(d.i), y=Y(v);
    if(!started){ g.moveTo(x,y); started=true; } else g.lineTo(x,y);
  });
  g.stroke();

  // 挂单（每轮 10 档，细横线；按当前方向着色）
  data.forEach(d=>{
    const x=X(d.i), col = d.s==="l"? COL.long : d.s==="s"? COL.short : "#484f58";
    g.strokeStyle=col; g.globalAlpha=0.42; g.lineWidth=1.6;
    d.b.forEach(px=>{
      const y=Y(px); if(y<PADT-2||y>PADT+ih+2) return;
      g.beginPath(); g.moveTo(x-2.2,y); g.lineTo(x+2.2,y); g.stroke();
    });
    g.globalAlpha=1;
  });

  // 成交
  p.fills.forEach(f=>{
    const x=X(f.i), y=Y(f.price);
    g.fillStyle = f.size>0? COL.long: COL.short;
    g.beginPath(); g.arc(x,y,2.2,0,7); g.fill();
  });

  // SL 触发
  p.sl.forEach(h=>{
    const x=X(h.i), y=Y(h.price);
    g.strokeStyle=COL.sl; g.lineWidth=1.6;
    g.beginPath(); g.moveTo(x-3,y-3); g.lineTo(x+3,y+3);
    g.moveTo(x+3,y-3); g.lineTo(x-3,y+3); g.stroke();
  });

  // 方向色带
  const bh=5, by=H-PADB+6;
  data.forEach((d,idx)=>{
    if(!d.s) return;
    const x0=X(d.i), x1 = idx<data.length-1? X(data[idx+1].i) : x0+iw/data.length;
    g.fillStyle = d.s==="l"? COL.long: COL.short;
    g.globalAlpha=0.85; g.fillRect(x0, by, Math.max(1,x1-x0), bh); g.globalAlpha=1;
  });

  // blocked 标记
  data.filter(d=>d.bl).forEach(d=>{
    g.fillStyle="#a371f7"; g.fillRect(X(d.i)-0.8, PADT, 1.6, 4);
  });
}

const EMA = P.ema;

function stats(p){
  const s=p.sum;
  return `<b>${s.auto_flips}</b> 次方向翻转 · <b>${s.fills}</b> 笔成交（<b>${s.fill_qty}</b> 张）
    · <b>${s.sl_hits}</b> 次 SL · 末仓 <b>${s.final_pos}</b> 张
    ${s.blocked_rounds? `· <span class="hl"><b>${s.blocked_rounds}</b> 轮被拦截</span>`:""}`;
}

const app=document.getElementById("app");
SEGS.forEach(([seg,a,b,title])=>{
  const wrap=document.createElement("div"); wrap.className="seg";
  wrap.innerHTML=`<h2>${title}</h2><div class="grid">
    <div class="card"><h3>线上版 a09bfc5 <span class="tag live">15m BB 裸判 · 无反向护栏</span></h3>
      <canvas id="cv_${seg}_live"></canvas><div class="stats" id="st_${seg}_live"></div></div>
    <div class="card"><h3>本地版 e5a9934 <span class="tag local">4H EMA200 迟滞 + 反向护栏</span></h3>
      <canvas id="cv_${seg}_local"></canvas><div class="stats" id="st_${seg}_local"></div></div>
  </div>`;
  app.appendChild(wrap);
});

requestAnimationFrame(()=>{
  SEGS.forEach(([seg])=>{
    ["live","local"].forEach(tag=>{
      const p=PANELS[`${seg}_${tag}`];
      draw(document.getElementById(`cv_${seg}_${tag}`), p);
      document.getElementById(`st_${seg}_${tag}`).innerHTML = stats(p);
    });
  });
});

const tot = ["live","local"].map(t=>SEGS.reduce((n,[s])=>n+PANELS[`${s}_${t}`].sum.auto_flips,0));
document.getElementById("note").innerHTML = `<b>怎么读这张图</b><br>
· 每个短横线是一次挂单，颜色随引擎判定的方向变（蓝=铺下方接多 / 橙=铺上方接空）。线上版的方向
在中轨附近来回跳（两段合计翻转 <b>${tot[0]}</b> 次），本地版始终单方向（<b>${tot[1]}</b> 次）——
因为线上 <code>direction_mode:auto</code> 读的是 15m BB 中轨，本地读 4H EMA200 带 0.98/1.005 迟滞。<br>
· 图底部色带同理：线上大面积蓝橙交替，本地一条色。<br>
· <b>本图不展示收益</b>：回放只有模板的共享 SL，<b>没有止盈</b>（TP 由模型每轮下发），
且未含资金费，所以成交与 SL 次数只反映“行为差异”，不代表盈亏。要看盈亏需 paper 实跑。`;
</script></body></html>"""

out = ROOT / "reports"
out.mkdir(exist_ok=True)
p = out / "replay_kline_chart.html"
html = HTML.replace("__PAYLOAD__", payload)
p.write_text(html, encoding="utf-8")
print("written:", p, len(html) // 1024, "KB")
