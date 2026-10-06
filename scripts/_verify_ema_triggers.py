"""4 个新 EMA 触发器 · 真实行情验证。

做法：取真实 BTC 15m K 线，**逐根回放**（第 i 次只喂到第 i 根），模拟
「每根 K 线检查一次条件」。这样触发/不触发都发生在真实价格上，而不是造数据。

每个类型跑三件事：
  1. 逐根回放 → 触发次数 / 触发率 / 首次触发的原文（证明真的会触发）
  2. 一次「故意不满足」的对照 → 必须为 False（证明不是恒真）
  3. 打印底层数值（EMA/偏离/斜率），便于手工核对判定
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.strategist.triggers import evaluate_condition  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402

paths = ProjectPaths(ROOT)
bot = load_bot_config(paths.config_dir / "brooks-btc.yaml")
client = bot.create_client()
SYM, TF = "BTC_USDT", "15m"

raw = client.get_klines(SYM, TF, 120)
print("真实 K 线: %s %s 共 %d 根" % (SYM, TF, len(raw)))
print("首根 %s" % (raw[0],))
print("末根 %s" % (raw[-1],))
print()


class Sliced:
    """只喂到第 n 根 —— 模拟「在第 n 根那一刻求值」。"""

    def __init__(self, rows, n):
        self._rows = rows[:n]

    def get_klines(self, symbol, interval, limit=None):
        return list(self._rows)

    def public_get(self, path, qs=""):
        return list(self._rows)


def scan(cond, lo=40, hi=None):
    """逐根回放，返回 (触发根索引列表, 首次触发的 reason)。"""
    hi = hi or len(raw)
    fires, first = [], ""
    for n in range(lo, hi + 1):
        ok, reason = evaluate_condition(Sliced(raw, n), dict(cond), TF)
        if ok:
            fires.append(n)
            if not first:
                first = reason
    return fires, first


T0 = 1_790_000_000.0


def scan_edge(cond, lo=40, hi=None):
    """逐根回放但走**真正的触发路径** `check_conditions`（共享 states）。

    这才是线上发生的事：每根 K 线检查一次，边沿触发 + 冷却都在起作用。
    """
    from omnialpha.strategist.triggers import check_conditions

    hi = hi or len(raw)
    states: dict = {}
    fires = []
    for n in range(lo, hi + 1):
        out = check_conditions(Sliced(raw, n), [dict(cond)], TF,
                               states=states, now=T0 + n * 900.0)
        if out:
            fires.append(n)
    return fires


CASES = [
    ("price_cross_ema", {"type": "price_cross_ema", "symbol": SYM, "period": 20, "dir": "any"},
     # 反向对照必须选一个**真的不满足**的方向：末根是上穿，所以 down 应为 False
     {"type": "price_cross_ema", "symbol": SYM, "period": 20, "dir": "down"}),
    ("ema_stack", {"type": "ema_stack", "symbol": SYM, "fast": 20, "slow": 50, "dir": "bull"},
     {"type": "ema_stack", "symbol": SYM, "fast": 20, "slow": 50, "dir": "bear"}),
    ("price_ema_dist", {"type": "price_ema_dist", "symbol": SYM, "period": 20, "pct": 0.05, "side": "above"},
     {"type": "price_ema_dist", "symbol": SYM, "period": 20, "pct": 15.0, "side": "above"}),
    ("ema_slope", {"type": "ema_slope", "symbol": SYM, "period": 20, "bars": 3, "dir": "up"},
     {"type": "ema_slope", "symbol": SYM, "period": 20, "bars": 3, "dir": "down"}),
]

total = len(raw) - 40 + 1
print("逐根回放窗口: 第 40 → %d 根，共 %d 次求值/类型" % (len(raw), total))
print("=" * 92)
for name, fire_cond, nofire_cond in CASES:
    fires, first = scan(fire_cond)
    edge_fires = scan_edge(fire_cond)
    ok_last, reason_last = evaluate_condition(Sliced(raw, len(raw)), dict(fire_cond), TF)
    ok_no, reason_no = evaluate_condition(Sliced(raw, len(raw)), dict(nofire_cond), TF)
    print("【%s】" % name)
    print("  触发条件   : %s" % {k: v for k, v in fire_cond.items() if k not in ("type", "symbol")})
    print("  逐根回放   : 判定为真 %d / %d 次（%.1f%%）" % (len(fires), total, 100.0 * len(fires) / total))
    print("  边沿触发   : 实际唤醒 %d / %d 次（%.1f%%）%s"
          % (len(edge_fires), total, 100.0 * len(edge_fires) / total,
             "  ← 压掉了 %d 次" % (len(fires) - len(edge_fires)) if len(fires) > len(edge_fires) else ""))
    if fires:
        print("  为真的根   : %s%s" % (fires[:12], " …" if len(fires) > 12 else ""))
        print("  首次触发   : %s" % first)
    print("  末根求值   : %s — %s" % (ok_last, reason_last))
    print("  反向对照   : %s — %s" % (ok_no, reason_no))
    print("  反向正确   : %s" % ("OK（不满足时为 False）" if not ok_no else "!! 反向也触发了，检查判据"))
    print("-" * 92)

# 底层数值，便于手工核对
closes = [float(r["c"]) if isinstance(r, dict) else float(r[4]) for r in raw]
from omnialpha.strategist.indicators import ema  # noqa: E402

e20, e50 = ema(closes, 20), ema(closes, 50)
print("底层数值（末根）: close=%.2f  EMA20=%.2f  EMA50=%.2f" % (closes[-1], e20[-1], e50[-1]))
print("  价格 vs EMA20      : %s" % ("上" if closes[-1] > e20[-1] else "下"))
print("  排列 price/EMA20/EMA50: %s" % ("多头" if closes[-1] > e20[-1] > e50[-1] else ("空头" if closes[-1] < e20[-1] < e50[-1] else "混合")))
print("  偏离 EMA20         : %+.3f%%" % ((closes[-1] - e20[-1]) / e20[-1] * 100))
print("  EMA20 近3根变化    : %+.2f" % (e20[-1] - e20[-4]))
print("  末根是否穿越 EMA20 : now_up=%s prev_up=%s" % (closes[-1] > e20[-1], closes[-2] > e20[-2]))
