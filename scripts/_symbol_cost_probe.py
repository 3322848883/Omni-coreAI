# -*- coding: utf-8 -*-
"""量「币种数 N」对成本的影响：REST 调用数 / 快照体积 / 图数。

不花 LLM token：只跑 collect_snapshot（真实取数）与 _generate_charts 的**计数**
（用假的渲染函数，不真画图）。用途：给「成本有界」这条不变量定上限。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\w6485\Desktop\测试\omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402
from omnialpha.strategist.snapshot import collect_snapshot  # noqa: E402
from omnialpha.strategist.market import MarketConfig  # noqa: E402

FIVE = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XAU_USDT", "XAG_USDT"]


def load_secrets() -> None:
    p = ROOT / "scripts" / "secrets.bat"
    if not p.is_file():
        return
    import os
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r'set\s+"?([A-Za-z_][A-Za-z0-9_]*)=(.*?)"?\s*$', line, re.I)
        if m and m.group(2).strip() and not os.environ.get(m.group(1)):
            os.environ[m.group(1)] = m.group(2).strip()


class CountingClient:
    """包一层真实 client，数调用次数与路径。"""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **kw):
            self.calls.append(name)
            return attr(*a, **kw)

        return wrapped


def main() -> int:
    load_secrets()
    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / "ab-multi-cur.yaml")
    mk = dict(bot.strategist.get("market") or {})
    cfg = MarketConfig(mode=str(mk.get("mode") or "rest_only"),
                       extra_timeframes=list(mk.get("timeframes") or []),
                       extra_candles=int(mk.get("extra_candles") or 80))
    print("market_cfg: mode=%s extra_timeframes=%s extra_candles=%s" % (
        cfg.mode, cfg.extra_timeframes, cfg.extra_candles))
    print()
    print("N  币种                                 REST调用  快照字节  ≈prompt token  图数(4N)")
    for n in (1, 2, 3, 5):
        syms = FIVE[:n]
        raw = bot.create_client()
        cc = CountingClient(raw)
        t0 = time.time()
        snap = collect_snapshot(cc, syms, candles=120, interval="5m",
                                market_cfg=cfg, env=bot.env, bot_root=ROOT)
        secs = time.time() - t0
        size = len(json.dumps(snap, ensure_ascii=False))
        print("%-3d %-35s %-9d %-9d %-14d %d   (%.1fs)" % (
            n, ",".join(s.split("_")[0] for s in syms), len(cc.calls), size,
            size // 4, 4 * n, secs))
    print()
    print("REST 调用明细（N=2 时）：")
    raw = bot.create_client()
    cc = CountingClient(raw)
    collect_snapshot(cc, FIVE[:2], candles=120, interval="5m", market_cfg=cfg,
                     env=bot.env, bot_root=ROOT)
    from collections import Counter
    for k, v in Counter(cc.calls).most_common():
        print("   %-28s %d" % (k, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
