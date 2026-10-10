# -*- coding: utf-8 -*-
"""`scripts/backfill_symbols.py` 的回填口径测试（审计 D-15 历史行收尾）。

覆盖四件不能出错的事：
- 从 `logs/trades/*.jsonl` 与 `archive/done/<bot>/*.result.json` 把 symbol / symbols_json
  补进**旧格式**（两列都是 NULL）的历史行，其它列逐字不变；
- 只补空：已经有值的行一个都不改；
- 幂等：重复跑第二次「0 行需要更新」；默认 dry-run 一行都不写；
- 来源缺失 / 匹配不上时只计入「跳过」，不报错、不猜。

夹具全在 tempfile 里造，不碰 `data/bots.db` 或任何生产数据。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from omnialpha.ledger import Ledger, default_ledger_path

ROOT = Path(__file__).resolve().parents[1]


def _load_mod():
    p = ROOT / "scripts" / "backfill_symbols.py"
    spec = importlib.util.spec_from_file_location("backfill_symbols", p)
    mod = importlib.util.module_from_spec(spec)
    # dataclass 处理要回查 `sys.modules[cls.__module__]`：不登记就 exec 会拿到 None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


BS = _load_mod()

# 除这两列以外的一切，回填前后必须逐字不变
MUTATED_COLS = ("symbol", "symbols_json")


def _steps(*symbols: str) -> list[dict]:
    """真实形态：`ExecReport.to_dict()` 把 symbol 放在**步骤顶层**（executor.py:72）。"""
    return [
        {"action": "open_long", "symbol": s, "ok": True, "detail": {"order": {"id": f"o{i}"}}}
        for i, s in enumerate(symbols)
    ]


def _jsonl_row(bot_id: str, steps: list[dict], *, plan_cycle: str = "c1",
               ts: float = 1000.0) -> dict:
    """`TradeLogger.log_execution` 的落盘字段集合（tradelog.py:47）。"""
    return {"ts": ts, "type": "execution", "bot_id": bot_id, "source": "watcher",
            "order_id": None, "plan_cycle": plan_cycle, "strategy": None,
            "ok": True, "steps": steps}


def _write_jsonl(root: Path, bot_id: str, rows: list[dict]) -> Path:
    d = root / "logs" / "trades"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{bot_id}.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                 encoding="utf-8")
    return p


def _write_result(root: Path, bot_id: str, steps: list[dict]) -> Path:
    d = root / "archive" / "done" / bot_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "sig.json.result.json"
    p.write_text(json.dumps({"ok": True, "steps": steps}, ensure_ascii=False), encoding="utf-8")
    return p


def _q(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _trades(db: Path) -> list[dict]:
    return _q(db, "SELECT * FROM trades ORDER BY id")


def _plans(db: Path) -> list[dict]:
    return _q(db, "SELECT * FROM plans ORDER BY id")


class BackfillCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.db = default_ledger_path(self.root)

    def tearDown(self):
        self._td.cleanup()

    # ---- 夹具：用 Ledger 建库（不传 symbols= 就是旧格式行：两列 NULL）
    def _legacy_trade(self, steps, *, bot_id="b1", plan_cycle="c1", ts=1000.0, **kw):
        led = Ledger(self.db)
        try:
            led.insert_trade(bot_id, plan_cycle=plan_cycle, action="execution", ok=True,
                             steps=steps, source="import", ts=ts, **kw)
        finally:
            led.close()

    def _legacy_plan(self, *, bot_id="b1", cycle_id="c1", raw=None, ts=1000.0):
        led = Ledger(self.db)
        try:
            led.insert_plan(bot_id, cycle_id=cycle_id, trigger="interval", orders=1,
                            raw=raw if raw is not None else {}, ts=ts)
        finally:
            led.close()

    def _run(self, **kw):
        return BS.run(self.root, **kw)


class TestDryRun(BackfillCase):
    def test_default_is_dry_run(self):
        """不带 --apply：统计出「将要更新」，但一行都不许写。"""
        self._legacy_trade(_steps("BTC_USDT"))
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT"))])

        res = self._run()

        self.assertFalse(res["apply"])
        self.assertEqual(res["tables"]["trades"]["to_update"], 1)
        self.assertEqual(res["applied"], 0)
        self.assertIsNone(_trades(self.db)[0]["symbol"], "dry-run 不得改动库")

    def test_cli_without_apply_does_not_write(self):
        """CLI 默认 dry-run —— 端到端跑一遍（子进程）也不许写库。"""
        self._legacy_trade(_steps("BTC_USDT"))
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT"))])
        cp = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "backfill_symbols.py"),
             "--root", str(self.root)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("DRY-RUN", cp.stdout)
        self.assertIsNone(_trades(self.db)[0]["symbol"])


class TestApply(BackfillCase):
    def test_fills_both_columns_and_touches_nothing_else(self):
        self._legacy_trade(_steps("BTC_USDT"))
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT"))])
        before = {k: v for k, v in _trades(self.db)[0].items() if k not in MUTATED_COLS}
        self.assertIsNone(_trades(self.db)[0]["symbols_json"], "夹具必须是旧格式行")

        res = self._run(apply=True)

        self.assertEqual(res["tables"]["trades"]["to_update"], 1)
        row = _trades(self.db)[0]
        self.assertEqual(row["symbol"], "BTC_USDT")
        self.assertEqual(json.loads(row["symbols_json"]), ["BTC_USDT"])
        after = {k: v for k, v in row.items() if k not in MUTATED_COLS}
        self.assertEqual(after, before, "除那两个列外一字不许改")

    def test_multi_symbol_matches_insert_trade_shape(self):
        """多币：symbol=逗号串、symbols_json=数组 —— 与 insert_trade 写新行同口径。"""
        self._legacy_trade(_steps("BTC_USDT", "ETH_USDT"))
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT", "ETH_USDT"))])

        self._run(apply=True)

        row = _trades(self.db)[0]
        self.assertEqual(row["symbol"], "BTC_USDT,ETH_USDT")
        self.assertEqual(json.loads(row["symbols_json"]), ["BTC_USDT", "ETH_USDT"])
        led = Ledger(self.db)
        try:
            self.assertEqual(len(led.recent_trades("b1", symbol="ETH_USDT")), 1,
                             "补完必须真的能按币筛出来（D-15 的验收形态）")
        finally:
            led.close()

    def test_same_payload_in_jsonl_and_result_json_fills_once(self):
        """同一笔执行在 jsonl 与 result.json 各有一份 —— 只补一次，另一份记为「重复」。"""
        self._legacy_trade(_steps("SOL_USDT"))
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("SOL_USDT"))])
        _write_result(self.root, "b1", _steps("SOL_USDT"))

        res = self._run(apply=True)

        self.assertEqual(res["tables"]["trades"]["to_update"], 1)
        # 两份来源里的第二份认不出「已经配过的那一行」→ 记「重复」，不是「匹配不上」
        self.assertEqual(sum(s["skipped_duplicate"] for s in res["sources"]), 1)
        self.assertEqual(_trades(self.db)[0]["symbol"], "SOL_USDT")


class TestIdempotent(BackfillCase):
    def test_second_run_updates_nothing(self):
        self._legacy_trade(_steps("BTC_USDT"))
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT"))])

        first = self._run(apply=True)
        second = self._run(apply=True)

        self.assertEqual(first["tables"]["trades"]["to_update"], 1)
        self.assertEqual(second["tables"]["trades"]["to_update"], 0, "第二次必须是 0 行需要更新")
        self.assertEqual(second["applied"], 0)
        self.assertEqual(_trades(self.db)[0]["symbol"], "BTC_USDT")

    def test_existing_value_is_never_overwritten(self):
        """已有值的行：即使来源 payload 对得上，也一个字段都不动。"""
        self._legacy_trade(_steps("BTC_USDT"), symbols=["ETH_USDT"])
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT"))])

        res = self._run(apply=True)

        row = _trades(self.db)[0]
        self.assertEqual(row["symbol"], "ETH_USDT")
        self.assertEqual(json.loads(row["symbols_json"]), ["ETH_USDT"])
        self.assertEqual(res["tables"]["trades"]["to_update"], 0)
        self.assertEqual(res["tables"]["trades"]["skipped_has_value"], 1)

    def test_partial_fill_only_fills_the_empty_column(self):
        """半空行（symbol 有值、symbols_json 为 NULL）只补空的那一列。"""
        self._legacy_trade(_steps("BTC_USDT"))
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE trades SET symbol='MANUAL_USDT' WHERE id=1")
            conn.commit()
        finally:
            conn.close()
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT"))])

        res = self._run(apply=True)

        row = _trades(self.db)[0]
        self.assertEqual(row["symbol"], "MANUAL_USDT")
        self.assertEqual(json.loads(row["symbols_json"]), ["BTC_USDT"])
        self.assertEqual(res["tables"]["trades"]["fills"]["symbol"], 0)
        self.assertEqual(res["tables"]["trades"]["fills"]["symbols_json"], 1)


class TestSkipping(BackfillCase):
    def test_no_sources_at_all_is_not_an_error(self):
        """jsonl / archive 都不存在：只报「跳过」，不抛异常、不猜。"""
        self._legacy_trade(_steps("BTC_USDT"))

        res = self._run(apply=True)

        self.assertEqual(res["sources"], [])
        self.assertEqual(res["tables"]["trades"]["to_update"], 0)
        self.assertEqual(res["tables"]["trades"]["db_unmatched"], 1)
        self.assertIsNone(_trades(self.db)[0]["symbol"])

    def test_unmatched_jsonl_rows_are_counted_not_guessed(self):
        self._legacy_trade(_steps("BTC_USDT"), plan_cycle="c1")
        # 币不同 → steps 摘要不同；plan_cycle 也不同 → 连唯一配对都够不上
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("DOGE_USDT"), plan_cycle="zz")])

        res = self._run(apply=True)

        src = [s for s in res["sources"] if s["path"].endswith(".jsonl")][0]
        self.assertEqual(src["skipped_unmatched"], 1)
        self.assertEqual(res["tables"]["trades"]["to_update"], 0)
        self.assertIsNone(_trades(self.db)[0]["symbol"])

    def test_broken_lines_are_skipped(self):
        self._legacy_trade(_steps("BTC_USDT"))
        p = _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT"))])
        p.write_text("{不是 JSON\n" + p.read_text(encoding="utf-8"), encoding="utf-8")

        res = self._run(apply=True)

        src = res["sources"][0]
        self.assertEqual(src["bad_lines"], 1)
        self.assertEqual(_trades(self.db)[0]["symbol"], "BTC_USDT")

    def test_bot_filter_limits_scope(self):
        self._legacy_trade(_steps("BTC_USDT"), bot_id="b1")
        self._legacy_trade(_steps("ETH_USDT"), bot_id="b2")
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("BTC_USDT"))])
        _write_jsonl(self.root, "b2", [_jsonl_row("b2", _steps("ETH_USDT"))])

        self._run(apply=True, bots=["b2"])

        rows = {r["bot_id"]: r for r in _trades(self.db)}
        self.assertIsNone(rows["b1"]["symbol"], "--bot b2 不许顺手动 b1")
        self.assertEqual(rows["b2"]["symbol"], "ETH_USDT")


class TestSources(BackfillCase):
    def test_archive_result_json_alone_is_enough(self):
        """没有 jsonl，只有归档 result.json（watcher.py:460 的产物）也能补。"""
        self._legacy_trade(_steps("XRP_USDT"))
        _write_result(self.root, "b1", _steps("XRP_USDT"))

        res = self._run(apply=True)

        self.assertEqual(res["tables"]["trades"]["by_method"], {"steps": 1})
        self.assertEqual(_trades(self.db)[0]["symbol"], "XRP_USDT")

    def test_v2_layout_paths_are_scanned(self):
        """v2 布局：data/bots/<bot>/logs/trades.jsonl（TradeLogger 现在写这里）。"""
        self._legacy_trade(_steps("TON_USDT"))
        p = self.root / "data" / "bots" / "b1" / "logs" / "trades.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_jsonl_row("b1", _steps("TON_USDT")), ensure_ascii=False) + "\n",
                     encoding="utf-8")

        self._run(apply=True)

        self.assertEqual(_trades(self.db)[0]["symbol"], "TON_USDT")

    def test_cycle_fallback_pairs_only_when_unique(self):
        """steps 对不上时的兜底：两侧都恰好 1 条才配对（宁漏补不张冠李戴）。"""
        self._legacy_trade([{"action": "open_long", "ok": True, "detail": {}}], plan_cycle="c9")
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("LTC_USDT"), plan_cycle="c9")])

        res = self._run(apply=True)

        self.assertEqual(res["tables"]["trades"]["by_method"], {"plan_cycle": 1})
        self.assertEqual(_trades(self.db)[0]["symbol"], "LTC_USDT")

    def test_cycle_fallback_refuses_ambiguous_groups(self):
        for _ in range(2):
            self._legacy_trade([{"action": "open_long", "ok": True, "detail": {}}], plan_cycle="c9")
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("LTC_USDT"), plan_cycle="c9")])

        res = self._run(apply=True)

        self.assertTrue(all(r["symbol"] is None for r in _trades(self.db)))
        src = res["sources"][0]
        self.assertEqual(src["skipped_ambiguous"], 1)

    def test_cycle_fallback_second_run_reports_has_value(self):
        """兜底补过的行，第二轮不该被记成「匹配不上」—— 那是已补上，不是没对上。"""
        self._legacy_trade([{"action": "open_long", "ok": True, "detail": {}}], plan_cycle="c9")
        _write_jsonl(self.root, "b1", [_jsonl_row("b1", _steps("LTC_USDT"), plan_cycle="c9")])
        self._run(apply=True)

        second = self._run(apply=True)

        self.assertEqual(second["tables"]["trades"]["to_update"], 0)
        self.assertEqual(second["sources"][0]["skipped_has_value"], 1)


class TestPlans(BackfillCase):
    def test_plan_symbols_from_jsonl_chips(self):
        self._legacy_plan(cycle_id="c1")
        row = {"ts": 1000.0, "type": "plan", "bot_id": "b1", "cycle_id": "c1",
               "orders": 1, "chips": [{"symbol": "BTC_USDT"}]}
        _write_jsonl(self.root, "b1", [row])

        res = self._run(apply=True)

        self.assertEqual(res["tables"]["plans"]["to_update"], 1)
        self.assertEqual(json.loads(_plans(self.db)[0]["symbols_json"]), ["BTC_USDT"])

    def test_plan_symbols_from_own_raw_json(self):
        """migrate 导入的历史 plan 行把整行塞进了 raw_json（migrate.py:89），自带 chips。"""
        self._legacy_plan(cycle_id="c1", raw={"type": "plan", "chips": [{"symbol": "ETH_USDT"}]})

        res = self._run(apply=True)

        self.assertEqual(res["tables"]["plans"]["by_method"], {"raw_json": 1})
        self.assertEqual(res["tables"]["plans"]["to_update"], 1, "库内自给的行也要计入「将更新」")
        self.assertEqual(res["planned_rows"], 1)
        self.assertEqual(json.loads(_plans(self.db)[0]["symbols_json"]), ["ETH_USDT"])

    def test_plan_without_any_symbol_source_stays_null(self):
        self._legacy_plan(cycle_id="c1")

        res = self._run(apply=True)

        self.assertEqual(res["tables"]["plans"]["to_update"], 0)
        self.assertEqual(res["tables"]["plans"]["db_unmatched"], 1)
        self.assertIsNone(_plans(self.db)[0]["symbols_json"])

    def test_plan_second_run_is_idempotent(self):
        self._legacy_plan(cycle_id="c1", raw={"chips": [{"symbol": "ETH_USDT"}]})
        self._run(apply=True)

        second = self._run(apply=True)

        self.assertEqual(second["tables"]["plans"]["to_update"], 0)

    def test_plan_jsonl_second_run_reports_has_value(self):
        self._legacy_plan(cycle_id="c1")
        row = {"ts": 1000.0, "type": "plan", "bot_id": "b1", "cycle_id": "c1",
               "orders": 1, "chips": [{"symbol": "BTC_USDT"}]}
        _write_jsonl(self.root, "b1", [row])
        self._run(apply=True)

        second = self._run(apply=True)

        self.assertEqual(second["tables"]["plans"]["to_update"], 0)
        self.assertEqual(second["sources"][0]["skipped_has_value"], 1)
        self.assertEqual(json.loads(_plans(self.db)[0]["symbols_json"]), ["BTC_USDT"])


if __name__ == "__main__":
    unittest.main()
