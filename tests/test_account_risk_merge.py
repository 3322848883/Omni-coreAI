"""账户级风控合并（多 bot 共账户时的合并闸门）。

回归背景（2026-10-05 架构盘点 S2/S6）：`account_risk` 逐 bot 配置、日初权益也
按 bot 落盘 —— 多 bot 共账户时各卡各自阈值，**合计敞口 ≈ N × 阈值**；
日亏熔断同账户下各自独立触发，不是账户级阈值。

修法：`config/accounts.yaml` 声明账户级 `account_risk`，bot 用 `account: <name>`
归属；合并时**上限类取更严的（min）**、`halt` 取逻辑或，其余 bot 级优先。
日初权益改走账户级共享路径，使日亏熔断成为账户级的。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import (  # noqa: E402
    load_accounts,
    load_all_bots,
    load_bot_config,
    merge_account_risk,
)


class TestMergeAccountRisk(unittest.TestCase):
    def test_caps_take_stricter(self):
        """上限类字段取更严（更小）的值。"""
        merged = merge_account_risk(
            {"max_total_notional_pct": 3.0, "max_leverage": 50},
            {"max_total_notional_pct": 10.0, "max_leverage": 20},
        )
        self.assertEqual(merged["max_total_notional_pct"], 3.0, "bot 更严 → 取 bot 的")
        self.assertEqual(merged["max_leverage"], 20, "账户级更严 → 取账户级的")

    def test_halt_is_logical_or(self):
        """任一为 true 就停手。"""
        self.assertTrue(merge_account_risk({}, {"halt": True})["halt"])
        self.assertTrue(merge_account_risk({"halt": True}, {"halt": False})["halt"])
        self.assertFalse(merge_account_risk({"halt": False}, {"halt": False})["halt"])

    def test_non_cap_fields_bot_wins(self):
        """非上限类（如 risk_pct）bot 级优先。"""
        merged = merge_account_risk({"risk_pct": 0.02}, {"risk_pct": 0.01})
        self.assertEqual(merged["risk_pct"], 0.02)

    def test_account_only_fields_kept(self):
        """账户级独有的字段要保留。"""
        merged = merge_account_risk({"risk_pct": 0.02},
                                    {"daily_loss_limit_usd": 20})
        self.assertEqual(merged["daily_loss_limit_usd"], 20)
        self.assertEqual(merged["risk_pct"], 0.02)


class TestLoadWithAccounts(unittest.TestCase):
    def _write(self, td, bot_yaml, accounts_yaml=None):
        cfg_dir = Path(td) / "config" / "bots"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "b1.yaml").write_text(bot_yaml, encoding="utf-8")
        if accounts_yaml is not None:
            (Path(td) / "config" / "accounts.yaml").write_text(
                accounts_yaml, encoding="utf-8")
        return cfg_dir

    def test_no_accounts_file_keeps_behavior(self):
        """没有 accounts.yaml 时行为完全不变（兼容性保证）。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._write(td, (
                "bot_id: b1\nenv: live\nsymbols: [BTC_USDT]\n"
                "account_risk: {max_total_notional_pct: 7.0}\n"))
            bot = load_bot_config(cfg / "b1.yaml")
            self.assertEqual(bot.account_risk["max_total_notional_pct"], 7.0)
            self.assertEqual(bot.account, "")

    def test_account_declared_merges(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._write(
                td,
                "bot_id: b1\nenv: live\nsymbols: [BTC_USDT]\naccount: gate-main\n"
                "account_risk: {max_total_notional_pct: 7.0}\n",
                "accounts:\n  gate-main:\n    account_risk:\n"
                "      max_total_notional_pct: 3.0\n      daily_loss_limit_usd: 20\n")
            accounts = load_accounts(cfg)
            self.assertIn("gate-main", accounts)
            bot = load_bot_config(cfg / "b1.yaml", accounts=accounts)
            self.assertEqual(bot.account, "gate-main")
            self.assertEqual(bot.account_risk["max_total_notional_pct"], 3.0)
            self.assertEqual(bot.account_risk["daily_loss_limit_usd"], 20)

    def test_load_all_bots_passes_accounts(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._write(
                td,
                "bot_id: b1\nenv: live\nsymbols: [BTC_USDT]\naccount: gate-main\n",
                "accounts:\n  gate-main:\n    account_risk:\n"
                "      max_leverage: 10\n")
            bots = load_all_bots(cfg)
            self.assertEqual(bots["b1"].account_risk["max_leverage"], 10)
            self.assertEqual(bots["b1"].account, "gate-main")

    def test_unknown_account_name_is_ignored(self):
        """声明了不存在的账户名 → 不合并、不报错（保持 bot 级）。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._write(
                td,
                "bot_id: b1\nenv: live\nsymbols: [BTC_USDT]\naccount: nope\n"
                "account_risk: {max_leverage: 33}\n",
                "accounts:\n  gate-main:\n    account_risk:\n      max_leverage: 10\n")
            bot = load_bot_config(cfg / "b1.yaml", accounts=load_accounts(cfg))
            self.assertEqual(bot.account, "nope")
            self.assertEqual(bot.account_risk["max_leverage"], 33)


class TestAccountDayStartEquity(unittest.TestCase):
    """配了 account 的 bot，日初权益要落在**账户级共享路径**。"""

    def test_account_path_shared(self):
        from omnialpha.executor import Executor

        class _C:
            def banner(self):
                return ""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = Executor(_C(), root=root, bot_id="b1", account="gate-main")
            ex._day_start_equity(1000.0)
            p = root / "data" / "accounts" / "gate-main" / "state"
            files = list(p.glob("equity_*.json"))
            self.assertEqual(len(files), 1, "应落在账户级路径")

    def test_no_account_keeps_bot_path(self):
        from omnialpha.executor import Executor

        class _C:
            def banner(self):
                return ""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = Executor(_C(), root=root, bot_id="b1")
            ex._day_start_equity(1000.0)
            p = root / "data" / "bots" / "b1" / "state" / "_account_risk"
            self.assertTrue(list(p.glob("equity_*.json")), "应落在 bot 级路径")
            self.assertFalse((root / "data" / "accounts").exists())


if __name__ == "__main__":
    unittest.main()
