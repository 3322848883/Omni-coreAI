#!/usr/bin/env python3
"""G1 挤压闸门本地 5 场景测试：模型是否按规则执行挤压状态判据。

场景（历史切片，非实时）：
  S1 挤压中 4h      （10-08 10:45）→ 期望：预警收紧，马丁最多再补 1 档
  S2 向下释放 1h     （10-08 12:00）→ 期望：撤马丁层，禁止新马丁
  S3 释放后未收复    （10-08 20:00，价格 2470 < 释放价 2538）→ 期望：仍禁马丁
  S4 已收复释放价    （10-12 08:00，价格 2576 > 释放价 2538）→ 期望：恢复正常
  S5 当下实况        （最新 bar）→ 期望：按实际状态执行
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.providers import resolve_llm_config  # noqa: E402
from omnialpha.strategist import build_system_prompt  # noqa: E402
from omnialpha.strategist.llm_client import LLMClient  # noqa: E402

OUT = Path("/tmp/sqz_g1_test")
OUT.mkdir(exist_ok=True)


def fetch(to=None, limit=300):
    qs = "contract=ETH_USDT&interval=15m&limit=1000" + (f"&to={to}" if to else "")
    rows = json.loads(urllib.request.urlopen(
        f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?{qs}", timeout=30).read())
    rows.sort(key=lambda r: int(r["t"]))
    return rows[-limit:]


def indicators(rows):
    closes = [float(r["c"]) for r in rows]
    highs = [float(r["h"]) for r in rows]
    lows = [float(r["l"]) for r in rows]
    n = len(closes)
    tr_l = [highs[0] - lows[0]]
    for i in range(1, n):
        pc = closes[i - 1]
        tr_l.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    out = []
    for i in range(19, n):
        w = closes[i - 19: i + 1]
        m = sum(w) / 20
        sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
        out.append({"boll20": round(m, 2), "boll_upper": round(m + 2 * sd, 2),
                    "boll_lower": round(m - 2 * sd, 2)})
    return out


def sqz_state(rows):
    """返回 (state, 挤压持续小时, 释放价或 None)。state: 1挤压 -1释放 0无。"""
    closes = [float(r["c"]) for r in rows]
    highs = [float(r["h"]) for r in rows]
    lows = [float(r["l"]) for r in rows]
    n = len(closes)
    tr_l = [highs[0] - lows[0]]
    for i in range(1, n):
        pc = closes[i - 1]
        tr_l.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    states = [None] * n
    for i in range(19, n):
        w = closes[i - 19: i + 1]
        m = sum(w) / 20
        sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
        ubb, lbb = m + 2 * sd, m - 2 * sd
        kma = sum(closes[i - 19: i + 1]) / 20
        rma = sum(tr_l[i - 19: i + 1]) / 20
        ukc, lkc = kma + 1.5 * rma, kma - 1.5 * rma
        on = (lbb > lkc) and (ubb < ukc)
        off = (lbb < lkc) and (ubb > ukc)
        states[i] = 1 if on else (-1 if off else 0)
    # 倒数：当前段状态与持续
    cur = states[-1]
    run = 0
    for s in reversed(states):
        if s == cur:
            run += 1
        else:
            break
    release_price = None
    if cur == -1:
        # 找最近的挤压→释放点
        for i in range(n - 1, 19, -1):
            if states[i] == -1 and states[i - 1] == 1:
                release_price = closes[i]
                break
    return cur, run * 0.25, release_price


def candle_json(rows, ind):
    tail = rows[-120:]
    ind_tail = ind[-120:]
    out = []
    for r, v in zip(tail, ind_tail):
        out.append({"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
                    "l": float(r["l"]), "c": float(r["c"]),
                    "boll20": v["boll20"], "boll_upper": v["boll_upper"],
                    "boll_lower": v["boll_lower"]})
    return out


bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
llm = LLMClient(resolve_llm_config(ROOT, bot_id="martin-grid",
                                   llm=(bot.strategist or {}).get("llm")))
base = (ROOT / "prompts/ladder_nogate.md").read_text(encoding="utf-8")
system = build_system_prompt(base, tools_guide="", skill_catalog=[])

# 固定账户状态（当前实盘布局）
ACCOUNT = """
持仓：ETH_USDT dual_long 6 张 @2346.75
挂单入场梯（下方）：网格 2316/2302/2288/2252/2222/2206 各1张；马丁 2278×6 / 2237×7 / 2190×9
保护：TP 2364.5(-6)；SL 2165(-6)；逐档TP 2405/2418/2433.5/2434.5/2454/2464 各-1张
（测试模式：快照与指标已直接注入，无需调用工具；按人格输出计划 JSON）
"""

SCENARIOS = [
    ("S1_挤压中4h", "2026-10-08 10:45:00"),
    ("S2_向下释放1h", "2026-10-08 12:00:00"),
    ("S3_释放后未收复", "2026-10-08 20:00:00"),
    ("S4_已收复释放价", "2026-10-12 08:00:00"),
    ("S5_当下实况", None),
]

EXPECT = {
    "S1_挤压中4h": "挤压预警：收紧马丁（最多再补1档），不得正常补马丁",
    "S2_向下释放1h": "撤马丁层/不挂新马丁",
    "S3_释放后未收复": "仍禁马丁（价格未收复释放价）",
    "S4_已收复释放价": "恢复正常（可低吸/维持阶梯）",
    "S5_当下实况": "按实际状态执行并声明",
}

only = [a for a in sys.argv[1:] if a.startswith("S")]
for name, tstr in SCENARIOS:
    if only and name.split("_")[0] not in only:
        continue
    t0 = time.time()
    if tstr:
        ts_t = int(time.mktime(time.strptime(tstr, "%Y-%m-%d %H:%M:%S"))) - time.timezone
        rows = fetch(to=ts_t + 1)
    else:
        rows = fetch()
    rows = [r for r in rows if int(r["t"]) <= (ts_t if tstr else 10 ** 12)]
    ind = indicators(rows)
    st, dur_h, rel_p = sqz_state(rows)
    st_txt = {1: "挤压中", -1: "已释放", 0: "无挤压"}.get(st, "?")
    cj = candle_json(rows, ind)
    last_c = float(rows[-1]["c"])
    user = f"""（G1 闸门测试 S：{name}）

测试模式说明：本轮无需调用工具，行情快照与挤压指标已直接注入。账户状态固定如下。
{ACCOUNT}
快照时刻：{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(int(rows[-1]['t'])))}，最新收盘 {last_c}

挤压指标（工具 sqzmom_state 计算结果，20/20/1.5TR）：
  sqzmom_state = {st}（{st_txt}），当前状态已持续 {dur_h:.2f} 小时
  释放价 = {rel_p if rel_p else 'N/A'}（挤压→释放那根的收盘价）

15m K 线（近 120 根，含 boll20/boll_upper/boll_lower）：
{json.dumps(cj, ensure_ascii=False)}

请按人格（含第十二、十三节）输出本轮计划 JSON。"""
    try:
        out = llm.chat(system, user)
        (OUT / f"{name}.txt").write_text(out, encoding="utf-8")
        acts = re.findall(r'"action":\s*"(\w+)"', out)
        # 马丁层 = 2278/2237/2190（或对应 6/7/9 张的新马丁档）
        has_open = any(a in ("open_long", "add_long") for a in acts)
        has_cancel = any(a in ("cancel_all", "cancel_price_all") for a in acts) or "撤" in out
        sqz_ref = ("挤压" in out) or ("sqz" in out.lower())
        print(f"{name}: {time.time()-t0:.0f}s  st={st}({dur_h:.1f}h)  "
              f"actions={acts[:6]}  提及挤压={sqz_ref}  有开仓腿={has_open}  有撤单={has_cancel}")
    except Exception as e:  # noqa: BLE001
        print(f"{name}: FAIL {type(e).__name__}: {str(e)[:90]}")
print("\noutputs →", OUT)
