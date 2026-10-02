"""trials 台账与 scoreboard 单测。"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.metrics.scoreboard import build_scoreboard, format_table  # noqa: E402
from omnialpha.metrics.trials import global_trial_n, scan_trials, trial_counts  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS pnl_snapshot (
 id INTEGER PRIMARY KEY, snap_time INTEGER, equity REAL, unrealised REAL, realised REAL, drawdown REAL);
CREATE TABLE IF NOT EXISTS fills (
 id INTEGER PRIMARY KEY, fill_time INTEGER, contract TEXT, side TEXT, price REAL,
 size REAL, fee REAL, realised_pnl REAL, role TEXT, order_id TEXT, kind TEXT);
"""


def _make_bot(root: Path, bid: str, env="paper", initial=10000):
    yml = root / "config" / "bots" / f"{bid}.yaml"
    yml.parent.mkdir(parents=True, exist_ok=True)
    yml.write_text(
        f"bot_id: {bid}\nenv: {env}\npaper:\n  initial_capital: {initial}\n"
        f"strategist:\n  prompt_file: prompts/{bid}.md\n",
        encoding="utf-8",
    )
    p = root / "prompts" / f"{bid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {bid}\n", encoding="utf-8")
    db = root / "data" / "bots" / bid / "paper" / "account.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(DDL)
    for i in range(20):
        con.execute(
            "INSERT INTO pnl_snapshot (snap_time, equity, unrealised, realised, drawdown) "
            "VALUES (?,?,?,?,0)",
            (1700000000 + i * 86400, 10000 + i * 10, 0.0, 0.0),
        )
    con.execute(
        "INSERT INTO fills (fill_time, contract, side, price, size, fee, realised_pnl, role, order_id, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1700000100, "BTC_USDT", "sell", 1, 1, 0.1, 5.0, "taker", "o1", "trade"),
    )
    con.commit()
    con.close()


class TestTrials(unittest.TestCase):
    def test_scan_counts_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_bot(root, "alpha")
            n1 = scan_trials(root)
            self.assertTrue(n1)
            c1 = trial_counts(root)
            self.assertGreaterEqual(c1.get("alpha", 0), 1)
            # 无变更 → 不增
            n2 = scan_trials(root)
            self.assertEqual(n2, [])
            c2 = trial_counts(root)
            self.assertEqual(c1, c2)
            # 改 yaml → +1
            yml = root / "config" / "bots" / "alpha.yaml"
            yml.write_text(yml.read_text(encoding="utf-8") + "label_prefix: a2\n", encoding="utf-8")
            n3 = scan_trials(root)
            self.assertTrue(any(r["kind"] == "config" for r in n3))
            self.assertGreater(trial_counts(root)["alpha"], c1["alpha"])
            self.assertGreaterEqual(global_trial_n(root), 2)


class TestScoreboard(unittest.TestCase):
    def test_two_bots_sorted_by_dsr(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_bot(root, "alpha")
            # beta 更差路径：持续下跌 → dsr 更低
            _make_bot(root, "beta")
            db = root / "data" / "bots" / "beta" / "paper" / "account.db"
            con = sqlite3.connect(db)
            con.execute("DELETE FROM pnl_snapshot")
            for i in range(20):
                con.execute(
                    "INSERT INTO pnl_snapshot (snap_time, equity, unrealised, realised, drawdown) "
                    "VALUES (?,?,?,?,0)",
                    (1700000000 + i * 86400, 10000 - i * 20, 0.0, 0.0),
                )
            con.commit()
            con.close()
            board = build_scoreboard(root)
            self.assertEqual(len(board["rows"]), 2)
            # days≈19 ≥3 → dsr 主序：上涨的 alpha 应在前
            self.assertEqual(board["rows"][0]["bot"], "alpha")
            self.assertGreater(board["rows"][0]["dsr"], board["rows"][1]["dsr"])
            text = format_table(board)
            self.assertIn("alpha", text)
            self.assertIn("beta", text)
            for r in board["rows"]:
                self.assertIn("dsr", r)
                self.assertIn("grade", r)
                self.assertIn("low_n", r)

    def test_missing_db_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yml = root / "config" / "bots" / "ghost.yaml"
            yml.parent.mkdir(parents=True, exist_ok=True)
            yml.write_text("bot_id: ghost\nenv: paper\n", encoding="utf-8")
            board = build_scoreboard(root)
            self.assertEqual(board["rows"][0]["grade"], "no_data")

    def test_shared_prompt_n_fans_out(self):
        """共享 prompt 变更应计入所有 owner bot 的 N。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared = root / "prompts" / "shared.md"
            shared.parent.mkdir(parents=True, exist_ok=True)
            shared.write_text("# shared\n", encoding="utf-8")
            for bid in ("a1", "a2"):
                yml = root / "config" / "bots" / f"{bid}.yaml"
                yml.parent.mkdir(parents=True, exist_ok=True)
                yml.write_text(
                    f"bot_id: {bid}\nenv: paper\nstrategist:\n  prompt_file: prompts/shared.md\n",
                    encoding="utf-8",
                )
            scan_trials(root)
            c1 = trial_counts(root)
            self.assertGreaterEqual(c1.get("a1", 0), 1)
            self.assertGreaterEqual(c1.get("a2", 0), 1)
            shared.write_text("# shared\nchanged\n", encoding="utf-8")
            scan_trials(root)
            c2 = trial_counts(root)
            self.assertGreater(c2["a1"], c1["a1"])
            self.assertGreater(c2["a2"], c1["a2"])


if __name__ == "__main__":
    unittest.main()
