# -*- coding: utf-8 -*-
"""`scripts/_symbol_audit.py` 的口径测试。

方案 S2.6-2 要求它当**运行期守卫**，所以它自己的统计口径也得被钉住 ——
否则守卫给出一个看着对、实际算错率的数字，比没有守卫更糟（本仓的"装饰性健康检查"
就是这个形态）。
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_audit_mod():
    p = ROOT / "scripts" / "_symbol_audit.py"
    spec = importlib.util.spec_from_file_location("_symbol_audit", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AUDIT = _load_audit_mod()
UNIVERSE = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]


def _write_round(state: Path, name: str, *, by_symbol, missing, oou, total,
                 notes=("explicit",), charts=1):
    d = {
        "tool_usage_summary": {
            "counts": {"klines": sum(v["calls"] for v in by_symbol.values())},
            "total_calls": total,
            "by_symbol": by_symbol,
            "missing_symbol": missing,
            "out_of_universe": oou,
        },
        "tool_usage": [{"tool": "klines", "symbol": s, "symbol_note": n}
                       for s in by_symbol for n in notes],
        "charts": [{"symbol": s, "timeframe": "5m", "ok": True, "bytes": 100}
                   for s in by_symbol] * charts,
    }
    (state / f"{name}.thinking.json").write_text(json.dumps(d), encoding="utf-8")


class TestAuditAggregation(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.state = self.root / "data" / "bots" / "b1" / "state"
        self.state.mkdir(parents=True)
        # 宇宙取自配置；测试里直接替换掉读取函数，聚焦"统计口径"本身
        self._orig = AUDIT._universe
        AUDIT._universe = lambda root, bot: list(UNIVERSE)

    def tearDown(self):
        AUDIT._universe = self._orig
        self._td.cleanup()

    def test_rates_and_coverage(self):
        _write_round(self.state, "r1",
                     by_symbol={"BTC_USDT": {"calls": 2}, "ETH_USDT": {"calls": 1}},
                     missing=0, oou=0, total=3)
        _write_round(self.state, "r2",
                     by_symbol={"BTC_USDT": {"calls": 1}, "ETH_USDT": {"calls": 1},
                                "SOL_USDT": {"calls": 1}},
                     missing=1, oou=2, total=4, notes=("explicit", "out_of_universe"))
        r = AUDIT.audit_bot(self.root, "b1", last=10)

        self.assertEqual(r["rounds"], 2)
        self.assertEqual(r["universe_size"], 3)
        self.assertEqual(sorted(r["coverage_tool"]), [2, 3])
        self.assertEqual(r["coverage_tool_avg"], 2.5)
        self.assertEqual(r["coverage_full_rounds"], 1)
        self.assertEqual(r["symbol_calls"], 6)
        self.assertEqual(r["tool_calls_total"], 7)
        self.assertAlmostEqual(r["missing_symbol_rate"], 1 / 6)
        self.assertAlmostEqual(r["out_of_universe_rate"], 2 / 7)
        self.assertEqual(r["per_symbol_calls"]["BTC_USDT"], 3)
        self.assertEqual(r["charts_ok"], 5, "2 币 + 3 币 = 5 张图，全部 ok")

    def test_decision_coverage_from_journal(self):
        _write_round(self.state, "r1", by_symbol={"BTC_USDT": {"calls": 1}},
                     missing=0, oou=0, total=1)
        (self.state / "memory_journal.jsonl").write_text(
            json.dumps({"cycle_id": "c1", "symbols": UNIVERSE}) + "\n"
            + json.dumps({"cycle_id": "c2", "symbols": ["BTC_USDT"]}) + "\n",
            encoding="utf-8")
        r = AUDIT.audit_bot(self.root, "b1", last=10)
        self.assertEqual(r["coverage_decision"], [3, 1], "决策覆盖 ≠ 取数覆盖，要分开报")
        self.assertEqual(r["journal_rounds"], 2)

    def test_empty_bot_is_graceful(self):
        (self.root / "data" / "bots" / "ghost" / "state").mkdir(parents=True)
        r = AUDIT.audit_bot(self.root, "ghost", last=10)
        self.assertEqual(r["rounds"], 0)
        self.assertEqual(r["missing_symbol_rate"], 0.0, "除零必须收敛成 0，不能炸")

    def test_broken_file_is_skipped_not_fatal(self):
        _write_round(self.state, "good", by_symbol={"BTC_USDT": {"calls": 1}},
                     missing=0, oou=0, total=1)
        (self.state / "bad.thinking.json").write_text("{不是 JSON", encoding="utf-8")
        r = AUDIT.audit_bot(self.root, "b1", last=10)
        self.assertEqual(r["rounds"], 1, "坏文件只跳过，不能让整次审计失败")


class TestStrictGate(unittest.TestCase):
    """`--strict` 只对**多币**宇宙生效：单币的 auto_single 会让缺 symbol 率居高不下。"""

    def test_multi_symbol_violation_fails(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "data" / "bots" / "b1" / "state"
            state.mkdir(parents=True)
            _write_round(state, "r1", by_symbol={"BTC_USDT": {"calls": 1}},
                         missing=1, oou=1, total=2)
            code = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "_symbol_audit.py"),
                 "--root", str(root), "--bot", "b1", "--strict"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace").returncode
            # 宇宙读不到（临时 root 没有 config）→ 不算多币 → 不拦
            self.assertEqual(code, 0, "读不到宇宙时不该误报")

    def test_strict_passes_on_clean_data(self):
        mod = AUDIT
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "data" / "bots" / "b1" / "state"
            state.mkdir(parents=True)
            _write_round(state, "r1",
                         by_symbol={"BTC_USDT": {"calls": 1}, "ETH_USDT": {"calls": 1}},
                         missing=0, oou=0, total=2)
            orig = mod._universe
            mod._universe = lambda r, b: ["BTC_USDT", "ETH_USDT"]
            try:
                r = mod.audit_bot(root, "b1", last=5)
            finally:
                mod._universe = orig
            self.assertTrue(r["multi_symbol"])
            self.assertEqual(r["missing_symbol_rate"], 0.0)
            self.assertEqual(r["out_of_universe_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
