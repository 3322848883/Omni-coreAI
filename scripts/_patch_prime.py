#!/usr/bin/env python3
"""修 ingest：v2/shadow 引擎首次写入前必须 prime（否则流状态为空 → 写全 NULL）。"""
import pathlib

p = pathlib.Path("pa-data-source/ingest.py")
s = p.read_text(encoding="utf-8")

old = """        w = self.writer(symbol, interval)
        if self.engine == "shadow":"""
new = """        w = self.writer(symbol, interval)
        # ── 首次写入前预热（2026-10-28 实测事故）────────────────────────
        # `prime()` 此前**没有任何调用者**：v2 引擎启动时 StreamState 是空的，
        # 于是前 20~200 根新 bar 的指标列全 NULL（实测 21:42/21:43 两根 0/7 填充）。
        # 每序列只在首写时 seed 一次，代价是读 warmup 根 OHLC。
        if not getattr(w, "_primed", False):
            try:
                seeded = self.prime(conn, symbol, interval)
                logger.info("primed %s %s: %d 根（engine=%s）",
                            symbol, interval, seeded, self.engine)
            except Exception as e:  # noqa: BLE001 — 预热失败不阻断写入
                logger.warning("prime %s %s failed: %s", symbol, interval, e)
            finally:
                w._primed = True

        if self.engine == "shadow":"""
assert old in s, "锚点未找到"
s = s.replace(old, new, 1)

if "import logging" not in s.split("class ")[0]:
    s = s.replace("import os", "import logging\nimport os", 1)
if "logger = logging.getLogger" not in s:
    s = s.replace("import os", "import os\n\nlogger = logging.getLogger(__name__)", 1)

p.write_text(s, encoding="utf-8")
print("ingest patched")
