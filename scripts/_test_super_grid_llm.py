# -*- coding: utf-8 -*-
"""超级网格人格 · 单轮 LLM 输出测试（真实数据 + 真实 LLM）。

流程：
  1. 拉 ETH 最新 K 线（15m/4h，Gate API）
  2. attach indicators（含 supertrend10_dir / supertrend10 / atr14 / boll）
  3. 真实算 smc_map / smc_events（15m）与挤压状态 —— 等价于 AI 查工具
  4. 独立算「标准答案」（4H ST 状态/确认根数/线距/5.1 权重），供对照
  5. system = prompts/super_grid.md 全文，user = 快照摘要 + 工具输出 + 输出形状
  6. 调 LLM（用户提供的测试网关）
  7. 解析 plan JSON，逐项核对人格硬规则

用法：python scripts/_test_super_grid_llm.py [base_url] [api_key] [model]
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "https://token.cgdoem.cn/v1"
API_KEY = sys.argv[2] if len(sys.argv) > 2 else ""
MODEL = sys.argv[3] if len(sys.argv) > 3 else "deepseek-v4.1-flash"

SYMBOL = "ETH_USDT"
MARK_TF = "15m"

# ── 1. 拉真实 K 线 ────────────────────────────────────────────────────
def fetch(interval: str, limit: int) -> list[dict]:
    os.environ.setdefault("no_proxy", "api.gateio.ws")
    u = (f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
         f"?contract={SYMBOL}&interval={interval}&limit={limit}")
    d = json.load(urllib.request.urlopen(u, timeout=30))
    return [{"t": int(x["t"]), "o": float(x["o"]), "h": float(x["h"]),
             "l": float(x["l"]), "c": float(x["c"]), "v": float(x["v"])}
            for x in d]

print("[1/6] 拉取真实 K 线 ...")
rows15 = fetch("15m", 300)
rows4h = fetch("4h", 120)
mark = rows15[-1]["c"]
print(f"      15m {len(rows15)} 根, 4h {len(rows4h)} 根, mark={mark}")

# ── 2. 指标 ──────────────────────────────────────────────────────────
print("[2/6] 计算指标 ...")
from omnialpha.strategist.indicators import attach_indicators  # noqa: E402

WANT = ["atr14", "boll20", "boll_upper", "boll_lower",
        "supertrend10", "supertrend10_dir"]
for rows in (rows15, rows4h):
    attach_indicators(rows, WANT)

a15 = rows15[-1]["atr14"]
bw15 = ((rows15[-1]["boll_upper"] - rows15[-1]["boll_lower"])
        / rows15[-1]["boll20"] * 100)

# ── 3. SMC 两件套 + 挤压（等价于 AI 查工具）──────────────────────────
print("[3/6] 计算 SMC 两件套与挤压 ...")
from omnialpha.strategist.smc_map import compute_smc_map, smc_map_summary  # noqa: E402
from omnialpha.strategist.smc_events import (  # noqa: E402
    compute_smc_events, smc_events_summary)
from omnialpha.strategist.indicators import squeeze_momentum  # noqa: E402

map15 = smc_map_summary(compute_smc_map(rows15))
map4h = smc_map_summary(compute_smc_map(rows4h))
evt15 = smc_events_summary(compute_smc_events(rows15))
sqz = squeeze_momentum([r["h"] for r in rows15], [r["l"] for r in rows15],
                       [r["c"] for r in rows15])
sqz_state = sqz["sqz_state"][-1]
sqz_run = 0
for v in reversed(sqz["sqz_state"]):
    if v == 1:
        sqz_run += 1
    else:
        break
sqz_txt = "无" if sqz_run == 0 else (f"进行中{sqz_run*15//60}小时" if sqz_run < 4 else f"进行中{sqz_run*15//60}小时")

# ── 4. 标准答案（独立计算，供对照）───────────────────────────────────
dirs4 = [r["supertrend10_dir"] for r in rows4h]
st4 = rows4h[-1]["supertrend10"]
atr4 = attach_indicators([dict(r) for r in rows4h], ["atr14"])[-1]["atr14"]
# 重新 attach（上面 dict 化会丢），直接从 rows4h 取
atr4 = rows4h[-1]["atr14"]
run4 = 0
for v in reversed(dirs4):
    if v == dirs4[-1]:
        run4 += 1
    else:
        break
just_flipped = run4 < 2
near_line = abs(mark - st4) < atr4
dist_line = round(mark - st4, 1)
if just_flipped:
    exp_w = "0.5 : 0.5（刚翻转，等确认）"
    exp_dir = "刚翻转"
elif dirs4[-1] == -1:
    exp_dir = "上涨趋势"
    w = 0.85 - (0.15 if near_line else 0.0)
    exp_w = f"{w:.2f} : {1-w:.2f}" + ("（贴线收敛）" if near_line else "")
else:
    exp_dir = "下跌趋势"
    w = 0.85 - (0.15 if near_line else 0.0)
    exp_w = f"{1-w:.2f} : {w:.2f}" + ("（贴线收敛）" if near_line else "")

print(f"      标准答案: 4H ST dir={dirs4[-1]} 维持{run4}根 ST线={st4} 距线{dist_line}"
      f" ATR4h={atr4} → {exp_dir}, 权重 {exp_w}")

# ── 5. 组装 prompt ───────────────────────────────────────────────────
print("[4/6] 组装 prompt ...")
system = (ROOT / "prompts" / "super_grid.md").read_text(encoding="utf-8")

recent = [{"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
          for r in rows15[-12:]]
user = f"""本轮快照（{SYMBOL}，15m 主周期，数据与工具输出已代查，禁止凭记忆）：

[账户]
equity: 100.00 USDT（paper）
positions: []            # 无持仓
ladder_levels: []        # 无已铺档位
open_orders: []          # 无挂单
protections: []          # 无保护单

[现价]
mark: {mark}

[方向引擎输入 — 4H SuperTrend]
4h 最新 supertrend10_dir: {dirs4[-1]}   （-1 涨 / 1 跌）
4h supertrend10_dir 序列（从旧到新，最近 12 根）: {dirs4[-12:]}
4h supertrend10 线值: {st4}
4h atr14: {atr4}

[15m 波动与区间]
15m atr14: {a15}
15m 布林带带宽: {bw15:.2f}%（P25≈0.7% / P50≈1.1% / P90≈2.9%）
15m boll20/boll_upper/boll_lower: {rows15[-1]['boll20']} / {rows15[-1]['boll_upper']} / {rows15[-1]['boll_lower']}

[挤压状态（G1，sqzmom 口径，已代查）]
sqz_state: {sqz_state}（1=挤压中 -1=已释放 0=无），连续挤压 {sqz_run} 根 → {sqz_txt}

[smc_map 15m 摘要（工具输出原文）]
{json.dumps(map15, ensure_ascii=False)[:3500]}

[smc_map 4h 摘要（工具输出原文）]
{json.dumps(map4h, ensure_ascii=False)[:2500]}

[smc_events 15m 摘要（工具输出原文）]
{json.dumps(evt15, ensure_ascii=False)[:3500]}

[15m 最近 12 根 OHLC]
{json.dumps(recent, ensure_ascii=False)}

---

按人格（system）输出本轮 plan。输出形状：单个 JSON 对象 {{"chips": [ ... ]}}。
- 无机会时 chips 为空、写一条 hold chip。
- 你不挂入场单（引擎按 meta.structure_levels 铺梯），需要输出的核心是：
  meta.structure_levels / meta.two_way / meta.ladder_step_pct（可选）/ 管理动作（本轮无仓位则无）。
- reasoning 首句按 5.5 格式。
"""

# ── 6. 调 LLM ────────────────────────────────────────────────────────
print(f"[5/6] 调 LLM（{MODEL} @ {BASE_URL}）... 这是 high reasoning，可能要几分钟")
body = {
    "model": MODEL,
    "messages": [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
    "temperature": 0.2,
    "max_tokens": 65536,  # 与生产一致：thinking 链就吃 16K+
    "response_format": {"type": "json_object"},
}
req = urllib.request.Request(
    f"{BASE_URL}/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Authorization": f"Bearer {API_KEY}",
             "Content-Type": "application/json"})
os.environ["no_proxy"] = "token.cgdoem.cn"
resp = json.load(urllib.request.urlopen(req, timeout=420))
content = resp["choices"][0]["message"]["content"]
usage = resp.get("usage", {})
Path(ROOT / "_probe" / "super_grid_out").mkdir(parents=True, exist_ok=True)
out_txt = ROOT / "_probe" / "super_grid_out" / "raw_response.json"
out_txt.write_text(json.dumps({"content": content, "usage": usage},
                              ensure_ascii=False, indent=1), encoding="utf-8")
print(f"      完成。prompt_tokens={usage.get('prompt_tokens')} "
      f"completion_tokens={usage.get('completion_tokens')}")
print("      原文已存", out_txt)

# ── 7. 核对 ──────────────────────────────────────────────────────────
print("[6/6] 核对人格硬规则 ...")
plan = json.loads(content)
chips = plan.get("chips") or []
report = []
r1 = content.strip().startswith("{")
report.append(("C1 合法 JSON", r1))

reasoning0 = (chips[0].get("reasoning") if chips else "") or ""
c2 = reasoning0.startswith("ST4H:")
report.append(("C2 首句 ST4H 格式", (c2, reasoning0[:60])))

two_way = (plan.get("meta") or {}).get("two_way") or {}
lw = (two_way.get("long") or {}).get("weight")
sw = (two_way.get("short") or {}).get("weight")
c3 = lw is not None and sw is not None
report.append(("C3 two_way 权重已给", (c3, f"long={lw} short={sw}")))

levels = (plan.get("meta") or {}).get("structure_levels") or []
c4 = len(levels) >= 8
report.append(("C4 structure_levels ≥8", (c4, f"{len(levels)} 个")))

reason_all = " ".join((c.get("reasoning") or "") for c in chips) + reasoning0
import re
m = re.search(r"结构位\s*(\d+)\s*[个+]?\s*(?:\+\s*)?ATR补档\s*(\d+)", reason_all)
report.append(("C5 ATR补档声明", (bool(m), m.group(0) if m else "未找到")))

c6 = all("confidence" in c for c in chips) and len(chips) > 0
report.append(("C6 confidence 全覆盖", (c6, f"{len(chips)} chips")))

hold = any((c.get("action") == "hold") for c in chips)
report.append(("C7 hold 或入场计划", (hold or len(chips) > 0, f"{len(chips)} chips")))

print()
print("=" * 62)
print("标准答案（独立计算）")
print(f"  4H ST: dir={dirs4[-1]} 维持{run4}根 ST线={st4} 现价距线{dist_line}点")
print(f"  判定: {exp_dir} → 权重 {exp_w}")
print(f"  15m: ATR={a15} 带宽={bw15:.2f}% 挤压={sqz_txt}")
print()
print("AI 核对")
ok = 0
for name, val in report:
    good = val if isinstance(val, bool) else val[0]
    detail = "" if isinstance(val, bool) else f"  {val[1]}"
    print(f"  [{'PASS' if good else 'FAIL'}] {name}{detail}")
    ok += bool(good)
print(f"  合计 {ok}/{len(report)}")
print("=" * 62)
print()
print("AI 输出原文（chips[0] 与 meta）：")
print(json.dumps({"meta": plan.get("meta"), "chips": chips[:3]},
                 ensure_ascii=False, indent=1)[:4000])
