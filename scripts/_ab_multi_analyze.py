# -*- coding: utf-8 -*-
"""多币种 A/B 结果分析：把「质量」拆成可机械核验的指标。

读 `_ab_multi/results/*.json`，用**真实 K 线**核对模型写出的价位（抓跨币泄漏/幻觉），
并统计覆盖率、工具取数覆盖、结构合规、风险预算、成本。

用法: python scripts/_ab_multi_analyze.py
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
OPEN_ACTIONS = ("open_long", "open_short", "stop_entry_long", "stop_entry_short")


def load_secrets() -> None:
    import os
    p = ROOT / "scripts" / "secrets.bat"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r'set\s+"?([A-Za-z_][A-Za-z0-9_]*)=(.*?)"?\s*$', line, re.I)
        if m and m.group(2).strip() and not os.environ.get(m.group(1)):
            os.environ[m.group(1)] = m.group(2).strip()


def bands() -> dict:
    """每个币的真实价格带（最近 200 根 5m）。"""
    load_secrets()
    from omnialpha.config import load_bot_config
    from omnialpha.watcher import ProjectPaths
    from omnialpha.strategist.market import fetch_rest_candles

    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / "ab-multi-cur.yaml")
    client = bot.create_client()
    out = {}
    for sym in FIVE:
        try:
            rows = fetch_rest_candles(client, sym, "5m", 200) or []
        except Exception as e:  # noqa: BLE001
            print("  取数失败 %s: %s" % (sym, e))
            continue
        if not rows:
            continue
        def num(r, *keys):
            for k in keys:
                if r.get(k) not in (None, ""):
                    return float(r[k])
            return None
        hi = max(x for x in (num(r, "h", "high") for r in rows) if x is not None)
        lo = min(x for x in (num(r, "l", "low") for r in rows) if x is not None)
        last = num(rows[-1], "c", "close") or hi
        out[sym] = {"lo": lo, "hi": hi, "last": last, "n": len(rows)}
    return out


def nums_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            v = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        if v >= 1:
            out.append(v)
    return out


def sim(a: str, b: str) -> float:
    """两个字符串的 token Jaccard 相似度（抓「复制粘贴」）。"""
    ta = set(re.findall(r"[A-Za-z0-9_.]+", (a or "").lower()))
    tb = set(re.findall(r"[A-Za-z0-9_.]+", (b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def main() -> int:
    print("取真实价格带…")
    B = bands()
    for s, v in B.items():
        print("  %-10s last=%-12.4f  区间=[%.4f, %.4f]  n=%d" % (s, v["last"], v["lo"], v["hi"], v["n"]))
    print()

    rows = []
    for fp in sorted(RES.glob("k*_*.json")):
        r = json.loads(fp.read_text(encoding="utf-8"))
        syms_cfg = r["symbols"]
        chips = r["plan_chips"]
        got = [c["symbol"] for c in chips]
        covered = [s for s in syms_cfg if s in got]
        missing = [s for s in syms_cfg if s not in got]

        # 工具取数覆盖
        tool_syms = set()
        for t in r["tools"]:
            s = t.get("symbol")
            if isinstance(s, list):
                tool_syms.update(s)
            elif s:
                tool_syms.add(str(s))
        no_data = [s for s in syms_cfg if s not in tool_syms]

        # 价格合规（真实 K 线核对）：该 chip 的所有价位必须落在它自己那个币的价格带
        bad_px, leak = [], []
        read_ok, read_tot = 0, 0
        for c in chips:
            b = B.get(c["symbol"])
            if not b:
                continue
            band_lo, band_hi = b["lo"] * 0.9, b["hi"] * 1.1
            for key in ("tp", "tp2", "sl", "price", "trigger_price"):
                v = c.get(key)
                if v in (None, "", 0):
                    continue
                if not (band_lo <= float(v) <= band_hi):
                    bad_px.append((c["symbol"], key, v))
                    other = [s for s, o in B.items() if s != c["symbol"]
                             and o["lo"] * 0.9 <= float(v) <= o["hi"] * 1.1]
                    if other:
                        leak.append((c["symbol"], key, v, other))
            for line in (c.get("kline_read") or []):
                vals = nums_in(line)
                for v in vals:
                    read_tot += 1
                    if band_lo <= v <= band_hi:
                        read_ok += 1

        # 结构合规
        struct_bad = []
        for c in chips:
            if c["action"] in OPEN_ACTIONS:
                if c.get("sl") in (None, "", 0):
                    struct_bad.append((c["symbol"], "开仓缺 sl"))
                if c.get("tp") in (None, "", 0):
                    struct_bad.append((c["symbol"], "开仓缺 tp"))
            if c.get("region") == "range" and c.get("tp2") not in (None, "", 0):
                struct_bad.append((c["symbol"], "range 却给了 tp2"))
            rl = c.get("reasoning") or ""
            if len(rl) > 40:
                struct_bad.append((c["symbol"], "reasoning 过长 %d" % len(rl)))

        # 跨币雷同（复制粘贴）
        worst_sim = 0.0
        for i in range(len(chips)):
            for j in range(i + 1, len(chips)):
                if chips[i]["symbol"] == chips[j]["symbol"]:
                    continue
                worst_sim = max(worst_sim, sim(chips[i].get("reasoning"), chips[j].get("reasoning")))

        u = r.get("usage") or {}
        rows.append({
            "k": r["k"], "arm": r["arm"],
            "chips": len(chips), "covered": len(covered), "missing": missing,
            "chip_syms": got,
            "tools": len(r["tools"]), "no_data_syms": no_data,
            "bad_px": bad_px, "leak": leak,
            "read_ok_ratio": (read_ok / read_tot) if read_tot else None,
            "struct_bad": struct_bad, "dup_sim": round(worst_sim, 3),
            "charts": r["charts"], "secs": r["elapsed_sec"],
            "tok": u.get("total_tokens"), "prompt_tok": u.get("prompt_tokens"),
            "err": r.get("error"),
        })

    print("== 覆盖率 / 成本 ==")
    print("k  arm  chips 覆盖  工具  无取数币  图  token   秒   跨币相似度")
    for x in rows:
        print("%-2d %-4s %-5d %d/%-4d %-4d %-9s %-3d %-7s %-5s %s" % (
            x["k"], x["arm"], x["chips"], x["covered"], x["k"], x["tools"],
            ",".join(s.split("_")[0] for s in x["no_data_syms"]) or "-",
            x["charts"], x["tok"], x["secs"], x["dup_sim"]))
    print()
    print("== 逐K读价格落在该币真实区间内的比例（越低=越可能读错标的/编数） ==")
    for x in rows:
        print("  k=%d %-4s chips=%-2d read_ok=%s" % (
            x["k"], x["arm"], x["chips"],
            ("%.0f%%" % (x["read_ok_ratio"] * 100)) if x["read_ok_ratio"] is not None else "-"))
    print()
    print("== 价格越界 / 跨币泄漏 ==")
    for x in rows:
        if x["bad_px"] or x["leak"]:
            print("  k=%d %s  越界=%s  泄漏=%s" % (x["k"], x["arm"], x["bad_px"][:4], x["leak"][:3]))
    print("  （无输出=两臂都没有越界价位）")
    print()
    print("== 结构违规 ==")
    for x in rows:
        if x["struct_bad"]:
            print("  k=%d %s  %s" % (x["k"], x["arm"], x["struct_bad"][:5]))
    print("  （无输出=没有违规）")
    print()
    print("== 每轮的 chip 摘要（symbol/action/confidence/region/kline_read 条数） ==")
    for fp in sorted(RES.glob("k*_*.json")):
        r = json.loads(fp.read_text(encoding="utf-8"))
        print("  k=%d %-4s %s" % (r["k"], r["arm"],
              " | ".join("%s:%s:%.2f:%s:%d" % (c["symbol"].split("_")[0], c["action"],
                                               c["confidence"] or 0, c["region"] or "-",
                                               c["kline_read_n"])
                         for c in r["plan_chips"]) or "(无 chip)"))
    print()
    for fp in sorted(RES.glob("k*_*.json")):
        r = json.loads(fp.read_text(encoding="utf-8"))
        if r.get("error"):
            print("  错误 k=%d %s: %s" % (r["k"], r["arm"], r["error"][:200]))
    out = RES / "_metrics.json"
    out.write_text(json.dumps({"bands": B, "rows": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("\n指标已落 %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
