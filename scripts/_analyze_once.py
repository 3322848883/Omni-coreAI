"""对某个 bot 跑一次**只读分析**并打印全过程。

用 `analyze_once` 而不是 `plan`：前者明确「LLM → Plan，**不写 inbox、不执行**」，
后者会把信号写进 inbox，被 `run` 进程消费就是真实下单。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True)
    ap.add_argument("--cot-chars", type=int, default=3000,
                    help="打印思维链末段的字符数")
    args = ap.parse_args()

    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / f"{args.bot}.yaml")
    from omnialpha.__main__ import _build_plan_runner
    runner = _build_plan_runner(bot, paths)

    print("=" * 92)
    print("bot=%s  env=%s  symbols=%s  timeframe=%s  label=%s" % (
        bot.bot_id, bot.env, bot.symbols, runner.cfg.timeframe, bot.label_prefix))
    print("prompt=%s  skills=%s" % (runner.cfg.prompt_file,
                                    runner.cfg.skills if runner.cfg.skills is not None
                                    else "(未设→全部可见)"))
    print("=" * 92)

    cap: dict = {}
    orig = runner._chat_with_tools

    def patched(system, user, chart_base64=None):
        cap["system"] = system
        cap["user"] = user
        # 图片是否真的随请求发出：`_chat_with_tools` 的第三个参数就是图（None = 无图）
        cap["chart_len"] = len(chart_base64) if chart_base64 else 0
        cap["calls"] = cap.get("calls", 0) + 1
        cap["text"] = orig(system, user, chart_base64=chart_base64)
        return cap["text"]

    runner._chat_with_tools = patched
    t0 = time.time()
    res = runner.analyze_once(trigger="manual-analyze-once") or {}
    secs = time.time() - t0

    usage = list(runner.tool_usage or [])
    print("\n耗时 %.1f 秒　工具 %d 个" % (secs, len(usage)))
    for u in usage:
        a = u.get("args") or {}
        print("  %-22s %-40s -> %d 字符" % (
            u.get("tool"), str(a)[:40], int(u.get("result_len") or 0)))
    reads = [u for u in usage if u["tool"] in ("skill", "skill_ref")]
    print("知识加载：%d 次，%d 字符" % (
        len(reads), sum(int(u.get("result_len") or 0) for u in reads)))

    # ── 图片 / 原始 K 线：模型实际收到了什么 ──
    sys_txt = cap.get("system") or ""
    usr_txt = cap.get("user") or ""
    print("\n" + "-" * 92)
    print("模型实际收到的输入")
    print("-" * 92)
    print("  LLM 调用次数      : %d" % cap.get("calls", 0))
    print("  system 长度       : %d 字符" % len(sys_txt))
    print("  user 长度         : %d 字符" % len(usr_txt))
    print("  **图片(chart_b64)**: %s" % (
        "无（未随请求发送）" if not cap.get("chart_len")
        else "%d 字符 base64" % cap["chart_len"]))
    kline_hits = [w for w in ("kline", "K线", "OHLC", "ohlc", "candles", "开盘", "最高", "最低", "收盘")
                  if w in usr_txt or w in sys_txt]
    print("  **原始K线痕迹**   : %s" % (kline_hits or "无"))
    # 快照里注入了多少根 K 线（从 user 里数时间戳/价格对这类特征较脆弱，
    # 改为直接看 snapshot 段落的规模）
    for tag in ("<snapshot", "snapshot", "行情", "klines"):
        i = usr_txt.find(tag) if tag in usr_txt else sys_txt.find(tag)
        if i >= 0:
            print("  命中 %-12s 于位置 %d" % (tag, i))
            break
    print("  user 前 400 字    : %s" % repr(usr_txt[:400]))

    rc = [str(x) for x in (getattr(runner.llm, "last_reasoning_chain", []) or [])]
    print("\n" + "-" * 92)
    print("思维链 %d 段，各段字符 %s" % (len(rc), [len(x) for x in rc]))
    print("-" * 92)
    if rc:
        n = max(0, args.cot_chars)
        seg = rc[-1]
        print(seg[-n:] if len(seg) > n else seg)

    print("\n" + "=" * 92)
    print("最终产出")
    print("=" * 92)
    print("ok=%s" % res.get("ok"))
    print(json.dumps(res.get("plan"), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
