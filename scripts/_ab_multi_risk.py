# -*- coding: utf-8 -*-
"""收尾指标：读数归属（剔时间戳）、跨币命中、每笔/每轮隐含风险。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RES = Path(r"C:\Users\w6485\Desktop\测试\_ab_multi\results")
EQUITY = 10000.0          # 测试 bot 的 paper 初始权益


def bands():
    return json.loads((RES / "_metrics.json").read_text(encoding="utf-8"))["bands"]


def nums(text, skip_ts=True):
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(text)):
        try:
            v = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        if v < 20:                      # 去掉「20 根」「18:15」这类小整数/小时
            continue
        if skip_ts and v >= 1_000_000:  # 去掉 epoch 时间戳
            continue
        yield v


def main() -> int:
    B = bands()
    print("== 读数归属（剔除时间戳与小整数） + 跨币命中 + 隐含风险 ==")
    print("k  arm  chip    symbol  read数  本币区间内  其他币区间内(泄漏)  隐含风险%  本轮合计风险%")
    for fp in sorted(RES.glob("k*_*.json")):
        r = json.loads(fp.read_text(encoding="utf-8"))
        k, arm = r["k"], r["arm"]
        raw = r.get("cot_tail") or ""
        # plan_chips 里有 reasoning/tp/sl/size（来自 to_signal_dict），读数用 thinking 原文
        think = None
        for p in (ROOT / "data" / "bots" / ("ab-multi-%s" % arm) / "state").glob("*.thinking.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            ts = float(d.get("ts") or 0)
            if float(r["ts"]) - 30 <= ts <= float(r["ts"]) + float(r["elapsed_sec"]) + 60:
                if think is None or abs(ts - float(r["ts"])) < abs(think[0] - float(r["ts"])):
                    think = (ts, d)
        chips_raw = []
        if think:
            t = (think[1].get("content_head") or "").strip()
            try:
                chips_raw = json.loads(t).get("chips") or []
            except Exception:  # noqa: BLE001
                chips_raw = []
        total_risk = 0.0
        for i, c in enumerate(chips_raw):
            sym = c.get("symbol") or "?"
            b = B.get(sym)
            own = other = 0
            if b:
                lo, hi = b["lo"] * 0.9, b["hi"] * 1.1
                for line in (c.get("kline_read") or []):
                    for v in nums(line):
                        if lo <= v <= hi:
                            own += 1
                        elif any(o["lo"] * 0.9 <= v <= o["hi"] * 1.1
                                 for s, o in B.items() if s != sym):
                            other += 1
            # 隐含风险：|entry - sl| / entry * size_usd / equity
            plan_c = next((x for x in r["plan_chips"] if x["symbol"] == sym), {})
            entry = plan_c.get("price") or plan_c.get("trigger_price")
            sl = plan_c.get("sl")
            size = plan_c.get("size_usd")
            risk = None
            if entry and sl and size:
                risk = abs(float(entry) - float(sl)) / float(entry) * float(size) / EQUITY * 100
                total_risk += risk
            print("  %-2d %-4s %-6d  %-6s  %-7d %-11d %-18d %-9s %s" % (
                k, arm, i, sym.split("_")[0], len(c.get("kline_read") or []), own, other,
                ("%.2f" % risk) if risk is not None else "-",
                "-" if risk is None else ""))
        print("        → k=%d %s 本轮隐含风险合计 = %.2f%%  （人格给的跨币上限 4%%）" % (k, arm, total_risk))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
