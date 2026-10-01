#!/usr/bin/env python3
"""
26步价格行为分析报告生成器（数据扫描骨架）
用法：python generate_analysis.py [symbol]
示例：python generate_analysis.py btc

环境变量：
  PRICE_ACTION_SKILL_ROOT     技能根目录（默认：脚本上级目录）
  PRICE_ACTION_DATA_DIR       K线 JSON 目录（默认：<skill>/data）
  PRICE_ACTION_OUTPUT_DIR     报告输出目录（默认：<skill>/logs/reports）

说明：完整 26 步分析需结合 references/SOUL.md + references/knowledge/ 规则引擎由主代理执行；
本脚本负责可复现的数据加载与市场状态摘要，避免硬编码个人路径。
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

SKILL_DIR = Path(os.environ.get(
    "PRICE_ACTION_SKILL_ROOT",
    Path(__file__).resolve().parents[1],
))
DATA_DIR = Path(os.environ.get(
    "PRICE_ACTION_DATA_DIR",
    SKILL_DIR / "data",
))
OUTPUT_DIR = Path(os.environ.get(
    "PRICE_ACTION_OUTPUT_DIR",
    SKILL_DIR / "logs" / "reports",
))


def load_kline_data(symbol, timeframe, limit=200):
    file_path = DATA_DIR / f"{symbol}_usdt_{timeframe}.json"
    if not file_path.exists():
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data[-limit:] if len(data) > limit else data


def load_memory_files():
    memory_dir = SKILL_DIR / "memory"
    files = {}
    key_files = [
        "error_patterns.md",
        "pattern_effectiveness.md",
        "trader_profile.md",
        "market_wisdom.md",
        "strategy_hypotheses.md",
        "market_state.md",
    ]
    for file_name in key_files:
        file_path = memory_dir / file_name
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            if "ERR-" in content:
                count = content.count("ERR-")
            elif "MEM-" in content:
                count = content.count("MEM-")
            elif "HYP-" in content:
                count = content.count("HYP-")
            else:
                count = 0
            files[file_name] = {"content": content, "count": count}
        else:
            files[file_name] = {"content": "", "count": 0}
    return files


def analyze_market_state(bars, timeframe):
    if not bars:
        return {"error": f"无数据（DATA_DIR={DATA_DIR}）"}

    latest = bars[-1]
    close = float(latest["c"])
    ema20 = float(latest["ema20"]) if latest.get("ema20") else None
    atr14 = float(latest["atr14"]) if latest.get("atr14") else None
    if not ema20:
        return {"error": "EMA20 数据不足"}

    valid_bars = [b for b in bars[-20:] if b.get("ema20")]
    above_ema = sum(1 for b in valid_bars if float(b["c"]) > float(b["ema20"]))
    ratio = above_ema / len(valid_bars) if valid_bars else 0

    if ratio > 0.6:
        ai_state = "AIL"
    elif ratio < 0.4:
        ai_state = "AIS"
    else:
        ai_state = "不确定"

    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    day_range = max(highs[-min(len(highs), 288):]) - min(lows[-min(len(lows), 288):])
    range_pct = (day_range / close * 100) if close else None
    if range_pct is None:
        vol = "unknown"
    elif range_pct > 8:
        vol = "极端"
    elif range_pct > 6:
        vol = "极端"
    elif range_pct > 4:
        vol = "高"
    elif range_pct > 2:
        vol = "常规"
    else:
        vol = "低"

    return {
        "timeframe": timeframe,
        "close": close,
        "ema20": ema20,
        "atr14": atr14,
        "ai_state": ai_state,
        "above_ema_ratio": round(ratio, 3),
        "range_pct": round(range_pct, 2) if range_pct is not None else None,
        "volatility": vol,
        "bars": len(bars),
    }


def build_report(symbol):
    now = datetime.now(timezone.utc)
    memory = load_memory_files()
    frames = {}
    for tf in ("4h", "1h", "5m"):
        bars = load_kline_data(symbol, tf)
        frames[tf] = analyze_market_state(bars, tf)

    lines = [
        f"# {symbol.upper()} 市场状态骨架报告",
        "",
        f"> 生成时间：{now.isoformat()}",
        f"> SKILL_DIR：{SKILL_DIR}",
        f"> DATA_DIR：{DATA_DIR}",
        "",
        "## 多时间框架状态",
        "",
        "| TF | close | EMA20 | AI | above_ema | range% | vol |",
        "|----|-------|-------|----|-----------|--------|-----|",
    ]
    for tf, st in frames.items():
        if "error" in st:
            lines.append(f"| {tf} | - | - | - | - | - | {st['error']} |")
        else:
            lines.append(
                f"| {tf} | {st['close']} | {st['ema20']} | {st['ai_state']} "
                f"| {st['above_ema_ratio']} | {st['range_pct']} | {st['volatility']} |"
            )

    lines += [
        "",
        "## 记忆文件摘要",
        "",
        "| 文件 | 条目信号 |",
        "|------|----------|",
    ]
    for name, meta in memory.items():
        lines.append(f"| {name} | {meta['count']} |")

    lines += [
        "",
        "## 后续（须由主代理按 references/SOUL.md 执行）",
        "",
        "1. 读取 references/SOUL.md + references/knowledge/workflow.md + strategy_workflow.md",
        "2. Step 0.5b 计划衔接（D/A1/A2/B/C）",
        "3. 完整 26 Steps（含 SB/CT/BAN 逐条、Step 4.0 止损校验、扣费方程）",
        "4. 输出可执行计划 + 引用清单",
        "5. 更新 memory/market_state.md 与 logs/",
        "",
        "本脚本不替代 26 步规则引擎分析。",
        "",
    ]
    return "\n".join(lines)


def main():
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "btc").lower()
    if not DATA_DIR.exists():
        print(f"ERROR: 数据目录不存在: {DATA_DIR}")
        print("请设置 PRICE_ACTION_DATA_DIR 指向 K 线 JSON 目录。")
        sys.exit(1)

    report = build_report(symbol)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_path = OUTPUT_DIR / f"{ts}_{symbol}_market_state.md"
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
