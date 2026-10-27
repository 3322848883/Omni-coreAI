# -*- coding: utf-8 -*-
"""watcher 接线冒烟：引擎开关、registry 构建、配置驱动列（不跑 WS）。

只验证「接线没接错」，不启动任何线程；真正的写入逻辑由 test_ingest 覆盖。
"""
import importlib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))


def _load_watcher():
    try:
        return importlib.import_module("kline_watcher")
    except Exception as e:  # pragma: no cover - 依赖缺失时跳过而非失败
        raise unittest.SkipTest(f"kline_watcher 不可导入: {e}")


class TestEngineHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PA_INDICATOR_ENGINE", None)
        os.environ.pop("PA_KLINE_V2", None)
        cls.kw = _load_watcher()
        cls.kw.load_config()   # 读 watchlist.yaml（本地存在）

    def tearDown(self):
        for k in ("PA_INDICATOR_ENGINE", "PA_KLINE_V2"):
            os.environ.pop(k, None)
        self.kw._INGEST_ENGINE = None
        self.kw._INGEST = None

    def test_default_engine_is_legacy(self):
        self.kw._INGEST_ENGINE = None
        self.assertEqual(self.kw._ingest_engine(), "legacy")

    def test_env_switch(self):
        self.kw._INGEST_ENGINE = None
        os.environ["PA_INDICATOR_ENGINE"] = "shadow"
        self.assertEqual(self.kw._ingest_engine(), "shadow")
        self.kw._INGEST_ENGINE = None
        os.environ["PA_INDICATOR_ENGINE"] = "v2"
        self.assertEqual(self.kw._ingest_engine(), "v2")

    def test_pa_kline_v2_shortcut(self):
        self.kw._INGEST_ENGINE = None
        os.environ["PA_KLINE_V2"] = "1"
        self.assertEqual(self.kw._ingest_engine(), "v2")

    def test_registry_uses_configured_indicators(self):
        self.kw._INGEST_ENGINE = None
        self.kw._INGEST = None
        reg = self.kw._get_ingest()
        self.assertEqual(reg.engine, "legacy")
        cols = [c for s in reg.specs for c in s.columns]
        # 默认 9 声明 → 11 列（ema20/50/200、atr14、rsi14、macd×3、boll×3）
        self.assertIn("ema200", cols)
        self.assertEqual(len(cols), 11)

    def test_failed_engine_resolution_falls_back(self):
        """引擎解析异常不能让采集崩：必须回落 legacy。"""
        self.kw._INGEST_ENGINE = None
        import ingest

        orig = ingest.resolve_engine
        ingest.resolve_engine = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(self.kw._ingest_engine(), "legacy")
        finally:
            ingest.resolve_engine = orig


if __name__ == "__main__":
    unittest.main()
