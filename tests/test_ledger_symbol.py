"""T15: ledger 写 symbol —— `trades.symbol` 非 NULL、`symbols_json` 可机读、查询可按币筛。

覆盖 spec `symbol-as-parameter.md` 的 T15 / S2.4⑩：
- `insert_trade`/`insert_plan` 接受 `symbols=` 并落 `symbols_json`（JSON 数组）；
- `symbol` 列：单币 = 该币，多币 = 逗号串（便于人读）；
- `recent_trades`/`recent_plans` 加 `symbol=` 过滤，且**不许**把 `BTC_USDT` 误匹配到 `BTC_USDTX`；
- 旧库（`CREATE TABLE` 时代建的、没有 `symbols_json` 列）打开后自动补列且不丢数据；
- 单币 trades.jsonl 逐字不变（新增维度只进 ledger，不进 jsonl）。

审计出处：D-15（`trades.symbol` 生产恒 NULL）、D-16（`plans` 无 symbol 列）、D-17（查询无按币过滤）。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.ledger import Ledger, default_ledger_path
from omnialpha.tradelog import TradeLogger, trade_log_path

# 旧库 schema（T15 之前的线上形态）：trades 有 symbol 列、plans 没有 symbols_json。
LEGACY_TRADES = (
    "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
    " bot_id TEXT NOT NULL, plan_cycle TEXT, action TEXT, symbol TEXT, size_usd REAL,"
    " price REAL, order_ids TEXT, ok INTEGER, steps_json TEXT, source TEXT);"
)
LEGACY_PLANS = (
    "CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
    " bot_id TEXT NOT NULL, cycle_id TEXT, trigger TEXT, orders INTEGER, notes TEXT,"
    " reasoning TEXT, raw_json TEXT);"
)
# 单币 execution 日志的既有字段集合 —— 一个字段都不许多、不许少。
JSONL_ROW_KEYS = {
    "ts", "type", "bot_id", "source", "order_id", "plan_cycle", "strategy", "ok", "steps",
}


def _steps(*symbols: str) -> list[dict]:
    """真实形态：`ExecReport.to_dict()` 把 symbol 放在**步骤顶层**（executor.py:72）。"""
    return [
        {"action": "open_long", "symbol": s, "ok": True, "detail": {"order": {"id": f"o{i}"}}}
        for i, s in enumerate(symbols)
    ]


def _exec(log: TradeLogger, bot_id: str, steps: list[dict]) -> None:
    with mock.patch("omnialpha.monitoring.notify_trade_events"):
        log.log_execution(bot_id, {"plan_cycle": "c1"}, {"ok": True, "steps": steps})


def _rows(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _columns(db: Path, table: str) -> set[str]:
    return {r[1] for r in _rows(db, f"PRAGMA table_info({table})")}


class TestTradeSymbol(unittest.TestCase):
    def test_log_execution_fills_symbol(self):
        """(a) 生产路径走一遍后 `trades.symbol` 必须非 NULL。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _exec(TradeLogger(trade_log_path(root, "b1")), "b1", _steps("BTC_USDT"))
            db = default_ledger_path(root)
            rows = _rows(db, "SELECT symbol, symbols_json FROM trades WHERE bot_id='b1'")
            self.assertEqual(len(rows), 1)
            self.assertIsNotNone(rows[0][0], "trades.symbol 不得为 NULL（审计 D-15）")
            self.assertEqual(rows[0][0], "BTC_USDT")
            self.assertEqual(json.loads(rows[0][1]), ["BTC_USDT"])

    def test_multi_symbol_json_and_filter(self):
        """(b) 多币 → symbols_json 是数组；按币能筛到，且不误匹配前缀币。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _exec(TradeLogger(trade_log_path(root, "b2")), "b2", _steps("BTC_USDT", "ETH_USDT"))
            led = Ledger(default_ledger_path(root))
            try:
                row = led.recent_trades("b2")[0]
                self.assertEqual(json.loads(row["symbols_json"]), ["BTC_USDT", "ETH_USDT"])
                self.assertEqual(row["symbol"], "BTC_USDT,ETH_USDT")
                self.assertEqual(len(led.recent_trades("b2", symbol="ETH_USDT")), 1)
                self.assertEqual(led.recent_trades("b2", symbol="BTC_USDTX"), [])
                self.assertEqual(len(led.recent_trades("b2", symbol="BTC_USDT")), 1)
                # 不带 symbol 过滤 = 原有行为
                self.assertEqual(len(led.recent_trades("b2")), 1)
            finally:
                led.close()

    def test_prefix_symbol_is_not_a_match(self):
        """`BTC_USDTX` 的记录不能被 `symbol="BTC_USDT"` 捞出来（LIKE 子串陷阱）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            led = Ledger(Path(root) / "bots.db")
            try:
                led.insert_trade("b1", action="open_long", symbols=["BTC_USDTX"], ok=True)
                self.assertEqual(led.recent_trades("b1", symbol="BTC_USDT"), [])
                self.assertEqual(len(led.recent_trades("b1", symbol="BTC_USDTX")), 1)
            finally:
                led.close()

    def test_missing_symbol_stays_empty(self):
        """提取不到 symbol 就留空 —— 不猜、不编造。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _exec(TradeLogger(trade_log_path(root, "b1")), "b1",
                  [{"action": "hold", "symbol": "", "ok": True, "detail": {"skipped": True}}])
            rows = _rows(default_ledger_path(root),
                         "SELECT symbol, symbols_json FROM trades WHERE bot_id='b1'")
            self.assertEqual(rows, [(None, None)])

    def test_single_symbol_dedups(self):
        """同一币多腿（如开仓 + 挂 SL）只记一次。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _exec(TradeLogger(trade_log_path(root, "b1")), "b1",
                  _steps("BTC_USDT", "BTC_USDT"))
            rows = _rows(default_ledger_path(root), "SELECT symbols_json FROM trades")
            self.assertEqual(json.loads(rows[0][0]), ["BTC_USDT"])


class TestPlanSymbol(unittest.TestCase):
    def test_insert_plan_symbols_json_and_filter(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td) / "bots.db")
            try:
                led.insert_plan("b1", cycle_id="c1", trigger="interval", orders=1,
                                symbols=["BTC_USDT", "ETH_USDT"])
                row = led.recent_plans("b1")[0]
                self.assertEqual(json.loads(row["symbols_json"]), ["BTC_USDT", "ETH_USDT"])
                self.assertEqual(len(led.recent_plans("b1", symbol="ETH_USDT")), 1)
                self.assertEqual(led.recent_plans("b1", symbol="ETH_USDTX"), [])
            finally:
                led.close()

    def test_plan_without_symbols_stays_null(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td) / "bots.db")
            try:
                led.insert_plan("b1", cycle_id="c1", trigger="interval", orders=0)
                self.assertIsNone(led.recent_plans("b1")[0]["symbols_json"])
            finally:
                led.close()


class TestLegacyDbUpgrade(unittest.TestCase):
    def _make_legacy_db(self, db: Path) -> None:
        conn = sqlite3.connect(str(db))
        try:
            conn.executescript(LEGACY_TRADES + LEGACY_PLANS)
            conn.execute(
                "INSERT INTO trades(ts,bot_id,plan_cycle,action,symbol,size_usd,price,"
                "order_ids,ok,steps_json,source) VALUES(1.0,'b1','c0','open_long','BTC_USDT',"
                "10.0,60000.0,'[]',1,'[]','run')")
            conn.execute(
                "INSERT INTO plans(ts,bot_id,cycle_id,trigger,orders,notes,reasoning,raw_json)"
                " VALUES(1.0,'b1','c0','interval',1,'[]','r','{}')")
            conn.commit()
        finally:
            conn.close()

    def test_old_db_gains_column_without_data_loss(self):
        """(c) 旧库（无 symbols_json 列）打开后自动补列，旧行原样保留。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "bots.db"
            self._make_legacy_db(db)
            self.assertNotIn("symbols_json", _columns(db, "trades"))
            led = Ledger(db)
            try:
                self.assertIn("symbols_json", _columns(db, "trades"))
                self.assertIn("symbols_json", _columns(db, "plans"))
                old = led.recent_trades("b1")
                self.assertEqual(len(old), 1)
                self.assertEqual(old[0]["symbol"], "BTC_USDT")
                self.assertIsNone(old[0]["symbols_json"])
                # 补列后新写入照常可用
                led.insert_trade("b1", action="open_long", symbols=["ETH_USDT"], ok=True)
                self.assertEqual(len(led.recent_trades("b1", symbol="ETH_USDT")), 1)
            finally:
                led.close()

    def test_reopen_is_idempotent(self):
        """重复打开（ALTER 再跑一次）不许报 duplicate column。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "bots.db"
            self._make_legacy_db(db)
            for _ in range(3):
                Ledger(db).close()
            self.assertIn("symbols_json", _columns(db, "trades"))

    def test_migrate_import_keeps_missing_symbol_empty(self):
        """迁移历史 trades.jsonl：记录里没有 symbol → 留空，不猜。"""
        import json as _json

        from omnialpha.migrate import migrate_bot

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trades = root / "logs" / "trades"
            trades.mkdir(parents=True)
            (trades / "b1.jsonl").write_text(
                _json.dumps({"type": "execution", "ok": True, "plan_cycle": "c1",
                             "steps": [{"action": "open_long", "ok": True,
                                        "detail": {"order": {"id": "o1"}}}]}) + "\n",
                encoding="utf-8")
            migrate_bot(root, "b1")
            rows = _rows(default_ledger_path(root),
                         "SELECT symbol, symbols_json FROM trades WHERE bot_id='b1'")
            self.assertEqual(rows, [(None, None)])

    def test_migrate_import_reads_symbol_from_steps(self):
        """历史 jsonl 的 steps 带 symbol 时应当被采用。"""
        import json as _json

        from omnialpha.migrate import migrate_bot

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trades = root / "logs" / "trades"
            trades.mkdir(parents=True)
            (trades / "b1.jsonl").write_text(
                _json.dumps({"type": "execution", "ok": True, "plan_cycle": "c1",
                             "steps": _steps("SOL_USDT")}) + "\n", encoding="utf-8")
            migrate_bot(root, "b1")
            rows = _rows(default_ledger_path(root),
                         "SELECT symbol, symbols_json FROM trades WHERE bot_id='b1'")
            self.assertEqual(rows[0][0], "SOL_USDT")
            self.assertEqual(json.loads(rows[0][1]), ["SOL_USDT"])


class TestSingleCoinJsonlUnchanged(unittest.TestCase):
    def test_jsonl_row_keys_unchanged(self):
        """单币 trades.jsonl 逐字不变：字段集合不许因 symbol 维度而变。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = TradeLogger(trade_log_path(root, "b1"))
            _exec(log, "b1", _steps("BTC_USDT"))
            line = (root / "data" / "bots" / "b1" / "logs" / "trades.jsonl").read_text(
                encoding="utf-8").strip()
            row = json.loads(line)
            self.assertEqual(set(row), JSONL_ROW_KEYS)
            self.assertEqual(row["type"], "execution")
            self.assertEqual(row["steps"], _steps("BTC_USDT"))


if __name__ == "__main__":
    unittest.main()
