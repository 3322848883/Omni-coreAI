"""对比测试：有图片 vs 无图片的 LLM 分析效果。

同一市场快照，分别用 vision（图+数据）和纯文本跑 LLM，比较分析质量。
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PYTHON = sys.executable
ENV = dict(os.environ)
ENV["PYTHONPATH"] = str(ROOT)

from gate_bot.strategist.vision import generate_and_encode
from gate_bot.strategist.indicators import ema
from gate_bot.strategist.llm_client import LLMClient, LLMConfig
from gate_bot.strategist.prompt import build_system_prompt, build_messages, load_strategy_prompt
from gate_bot.strategist.snapshot import collect_snapshot
from gate_bot.gate_client import GateClient


def fetch_klines(interval="1h", limit=100):
    import urllib.request
    url = f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=BTC_USDT&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def run_analysis(with_image: bool) -> dict:
    """跑一轮 LLM 分析。"""
    # 拉数据
    all_k = fetch_klines("1h", 120)  # 100 显示 + 20 warmup
    warmup = all_k[:20]
    display = all_k[20:]
    closes = [float(k["c"]) for k in all_k]
    ema20_full = ema(closes, 20)
    ema20_display = ema20_full[20:]

    # 生成图（vision 模式）
    chart_b64 = None
    if with_image:
        chart_b64 = generate_and_encode(
            display, symbol="BTC_USDT", timeframe="1h",
            output_path=ROOT / "data" / f"cmp_{'with' if with_image else 'without'}.png",
        )

    # 市场快照
    client = GateClient("", "")
    snapshot = {
        "ticker": {"last": display[-1]["c"], "high_24h": display[-1]["h"],
                    "low_24h": display[-1]["l"]},
        "klines_count": len(display),
    }

    # 构建 prompt
    strategy = load_strategy_prompt(None)
    system = build_system_prompt(strategy)
    risk = {"min_confidence": 0.3, "max_notional_usd": 3000, "max_chips": 1}
    symbols = ["BTC_USDT"]

    system_msg, user_content = build_messages(
        strategy, snapshot, risk, symbols,
        chart_base64=chart_b64,
    )

    # 调 LLM
    llm_cfg = LLMConfig()
    llm = LLMClient(llm_cfg)

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content},
    ]

    t0 = time.time()
    try:
        if hasattr(llm, "chat_messages"):
            text = llm.chat_messages(messages)
        else:
            text = llm.chat(system_msg, str(user_content))
    except Exception as e:
        text = f"ERROR: {e}"
    elapsed = time.time() - t0

    return {
        "with_image": with_image,
        "text": text,
        "elapsed_s": round(elapsed, 1),
        "chart_bytes": len(chart_b64) if chart_b64 else 0,
    }


def compare(r_with: dict, r_without: dict):
    """对比分析质量。"""
    print("=" * 60)
    print("对比结果")
    print("=" * 60)

    # 字数
    len_with = len(r_with["text"])
    len_without = len(r_without["text"])
    print(f"\n分析长度: 有图={len_with}字 vs 无图={len_without}字")

    # 耗时
    print(f"LLM 耗时: 有图={r_with['elapsed_s']}s vs 无图={r_without['elapsed_s']}s")

    # 关键词覆盖
    keywords = ["EMA", "均线", "趋势", "支撑", "压力", "突破", "形态", "反转",
                "动量", "成交量", "超买", "超卖", "背离", "K线", "蜡烛", "影线"]
    for kw in keywords:
        w = kw in r_with["text"]
        wo = kw in r_without["text"]
        if w or wo:
            status = "✓" if w else "✗"
            status2 = "✓" if wo else "✗"
            print(f"  {kw:4s}: 有图{status}  无图{status2}")

    print(f"\n--- 有图分析（前 500 字）---")
    print(r_with["text"][:500])
    print(f"\n--- 无图分析（前 500 字）---")
    print(r_without["text"][:500])


if __name__ == "__main__":
    print("开始对比测试...")
    print("1) 无图模式（纯文本）...")
    r_without = run_analysis(with_image=False)
    print(f"   完成: {r_without['elapsed_s']}s, {len(r_without['text'])}字")

    print("2) 有图模式（图+数据）...")
    r_with = run_analysis(with_image=True)
    print(f"   完成: {r_with['elapsed_s']}s, {len(r_with['text'])}字")

    compare(r_with, r_without)

    # 保存完整结果
    result_path = ROOT / "data" / "vision_comparison.json"
    result_path.write_text(json.dumps({
        "with_image": r_with, "without_image": r_without,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整结果: {result_path}")
