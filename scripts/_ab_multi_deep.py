# -*- coding: utf-8 -*-
"""从 thinking.json 的**模型原文**核算逐K读合规与跨币泄漏。

`analyze_once` 的返回值是 `Chip.to_signal_dict()` 序列化的，**故意不含**
`kline_tf`/`kline_read`（那两个字段只作审计落 thinking.json）。所以要看模型
到底有没有按人格要求写 20 条逐K读、以及那些读数是不是**这个币**的，必须回到原文。

用法: python scripts/_ab_multi_deep.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RES = Path(r"C:\Users\w6485\Desktop\测试\_ab_multi\results")
FIVE = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XAU_USDT", "XAG_USDT"]


def _bands() -> dict:
    m = json.loads((RES / "_metrics.json").read_text(encoding="utf-8"))
    return m["bands"]


def raw_json_of(bot: str, t0: float, t1: float):
    """按时间窗取该轮 writing 的模型原文（thinking.json）。"""
    best = None
    for p in (ROOT / "data" / "bots" / bot / "state").glob("*.thinking.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        ts = float(d.get("ts") or 0)
        if t0 - 30 <= ts <= t1 + 60:
            if best is None or abs(ts - t0) < abs(best[0] - t0):
                best = (ts, d, p.name)
    return best


def parse_raw(text: str):
    """从模型原文里抠出 chips（含 kline_read/kline_tf）。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        d = json.loads(t)
        return d.get("chips") or [], "json"
    except Exception:  # noqa: BLE001
        pass
    # 退化：正则抓 kline_read 数组与 kline_tf
    chips = []
    for m in re.finditer(r'"symbol"\s*:\s*"([A-Z0-9_]+)"', t):
        chips.append({"symbol": m.group(1)})
    return chips, "regex(截断)"


def main() -> int:
    B = _bands()
    print("== 从模型原文核算逐K读（人格硬要求：kline_read 恰好 20 条 + 写明 kline_tf）==")
    print("k  arm  chip   symbol     kline_tf  read条数  读数落在该币区间内  越界示例")
    for fp in sorted(RES.glob("k*_*.json")):
        r = json.loads(fp.read_text(encoding="utf-8"))
        arm, k = r["arm"], r["k"]
        bot = "ab-multi-%s" % arm
        got = raw_json_of(bot, float(r["ts"]), float(r["ts"]) + float(r["elapsed_sec"]))
        if not got:
            print("  k=%d %-4s (找不到对应 thinking.json)" % (k, arm))
            continue
        chips, how = parse_raw(got[1].get("content_head") or "")
        if not chips:
            print("  k=%d %-4s (原文解析失败 %s)" % (k, arm, how))
            continue
        for c in chips:
            sym = c.get("symbol") or "?"
            kr = c.get("kline_read")
            n = len(kr) if isinstance(kr, list) else (0 if kr in (None, "") else 1)
            b = B.get(sym)
            ok = bad = 0
            sample = ""
            if b and isinstance(kr, list):
                lo, hi = b["lo"] * 0.9, b["hi"] * 1.1
                for line in kr:
                    for tok in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(line)):
                        try:
                            v = float(tok.group(0).replace(",", ""))
                        except ValueError:
                            continue
                        if v < 1:
                            continue
                        if lo <= v <= hi:
                            ok += 1
                        else:
                            bad += 1
                            if not sample:
                                sample = "%s(应为 %.1f~%.1f)" % (tok.group(0), b["lo"], b["hi"])
            ratio = ("%d/%d" % (ok, ok + bad)) if (ok or bad) else "-"
            print("  %-2d %-4s %-5d %-9s %-8s %-8d %-19s %s" % (
                k, arm, chips.index(c), sym.split("_")[0], c.get("kline_tf") or "-", n, ratio, sample))
        print("     （%s，原文 %d 字）" % (how, len(got[1].get("content_head") or "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
