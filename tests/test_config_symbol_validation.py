"""配置层 symbol 归一与 fail-fast（T4）。

背景（设计文档 S1-P1 / C-1 / C-7 / A-2 / A-15）：配置里的币种此前**完全不归一、不校验**
——小写或裸写原样进快照、`label_prefix` 重名可跨 bot 撤单、`symbols: []` 被静默理解成
「不限制」，全是「配置看起来生效、实际不生效」。这里把每一条都变成**启动即报错**。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha import config as cfgmod  # noqa: E402
from omnialpha.config import (  # noqa: E402
    BotConfig,
    GateApiError,
    assert_account_risk_consistent,
    load_all_bots,
    load_bot_config,
    normalize_symbols,
)

BASE = """\
bot_id: {bid}
env: paper
symbols: {symbols}
label_prefix: {label}
{extra}
"""


def _make(tmp: Path, bots: dict[str, str], enabled: list[str] | None = None) -> Path:
    """建一个 config/bots + config/bots.local 目录树（enabled 只认 overlay）。"""
    cfg = tmp / "config" / "bots"
    cfg.mkdir(parents=True, exist_ok=True)
    for name, text in bots.items():
        (cfg / f"{name}.yaml").write_text(text, encoding="utf-8")
    if enabled:
        ov = tmp / "config" / "bots.local"
        ov.mkdir(parents=True, exist_ok=True)
        for name in enabled:
            (ov / f"{name}.yaml").write_text("enabled: true\n", encoding="utf-8")
    return cfg


class TestNormalizeSymbols(unittest.TestCase):
    def test_normalize_dedup_and_order(self):
        self.assertEqual(
            normalize_symbols(["btcusdt", "BTC_USDT", "eth", " eth ", "XAU"]),
            ["BTC_USDT", "ETH_USDT", "XAU_USDT"],
        )

    def test_empty_sequence(self):
        self.assertEqual(normalize_symbols([]), [])
        self.assertEqual(normalize_symbols(None), [])

    def test_blank_entry_rejected(self):
        with self.assertRaises(GateApiError):
            normalize_symbols(["BTC_USDT", "  "])


class TestSymbolsInConfig(unittest.TestCase):
    def test_config_normalizes_symbols(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[btcusdt, BTC_USDT, eth]", label="b1", extra="")})
            bot = load_bot_config(cfg / "b1.yaml")
            self.assertEqual(bot.symbols, ["BTC_USDT", "ETH_USDT"])

    def test_strategist_symbols_normalized(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[btcusdt]", label="b1",
                extra="strategist:\n  symbols: [btcusdt]\n")})
            bot = load_bot_config(cfg / "b1.yaml")
            self.assertEqual(bot.strategist["symbols"], ["BTC_USDT"])

    def test_strategist_universe_must_be_subset(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[btcusdt]", label="b1",
                extra="strategist:\n  symbols: [eth]\n")}, enabled=["b1"])
            with self.assertRaises(GateApiError) as ctx:
                load_all_bots(cfg)
            msg = str(ctx.exception)
            self.assertIn("ETH_USDT", msg)
            self.assertIn("BTC_USDT", msg)

    def test_strategist_subset_ok_when_disabled(self):
        """未启用的 bot 只告警不阻断（别拦住别人的实验配置）。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[btcusdt]", label="b1",
                extra="strategist:\n  symbols: [eth]\n")})
            bots = load_all_bots(cfg)
            self.assertEqual(bots["b1"].symbols, ["BTC_USDT"])


class TestEmptySymbols(unittest.TestCase):
    def test_empty_symbols_raises_when_enabled(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[]", label="b1", extra="")}, enabled=["b1"])
            with self.assertRaises(GateApiError) as ctx:
                load_all_bots(cfg)
            self.assertIn("symbols_unrestricted", str(ctx.exception))

    def test_empty_symbols_ok_with_explicit_switch(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[]", label="b1",
                extra="symbols_unrestricted: true\n")}, enabled=["b1"])
            bots = load_all_bots(cfg)
            self.assertEqual(bots["b1"].symbols, [])
            self.assertTrue(bots["b1"].symbols_unrestricted)

    def test_empty_symbols_only_warns_when_disabled(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[]", label="b1", extra="")})
            self.assertEqual(load_all_bots(cfg)["b1"].symbols, [])

    def test_unrestricted_switch_conflicts_with_list(self):
        """两个开关同时给 = 语义矛盾，必须报错（新字段，无既有用法）。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[BTC_USDT]", label="b1",
                extra="symbols_unrestricted: true\n")})
            with self.assertRaises(GateApiError):
                load_bot_config(cfg / "b1.yaml")


class TestLabelPrefixUnique(unittest.TestCase):
    def test_duplicate_prefix_raises_listing_bots(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {
                "b1": BASE.format(bid="b1", symbols="[BTC_USDT]", label="dup", extra=""),
                "b2": BASE.format(bid="b2", symbols="[ETH_USDT]", label="dup", extra=""),
            }, enabled=["b1", "b2"])
            with self.assertRaises(GateApiError) as ctx:
                load_all_bots(cfg)
            msg = str(ctx.exception)
            self.assertIn("dup", msg)
            self.assertIn("b1", msg)
            self.assertIn("b2", msg)

    def test_one_enabled_one_disabled_only_warns(self):
        """仓库实况：`ofl`/`wyk`/`sc` 三对重名里各只有一个在跑 → 不得误报。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {
                "b1": BASE.format(bid="b1", symbols="[BTC_USDT]", label="dup", extra=""),
                "b2": BASE.format(bid="b2", symbols="[ETH_USDT]", label="dup", extra=""),
            }, enabled=["b1"])
            bots = load_all_bots(cfg)
            self.assertEqual(sorted(bots), ["b1", "b2"])

    def test_distinct_prefixes_ok(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {
                "b1": BASE.format(bid="b1", symbols="[BTC_USDT]", label="one", extra=""),
                "b2": BASE.format(bid="b2", symbols="[ETH_USDT]", label="two", extra=""),
            }, enabled=["b1", "b2"])
            self.assertEqual(sorted(load_all_bots(cfg)), ["b1", "b2"])


class TestAccountScope(unittest.TestCase):
    def test_default_is_auto(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[BTC_USDT]", label="b1", extra="")})
            self.assertEqual(load_bot_config(cfg / "b1.yaml").account_scope, "auto")

    def test_explicit_values(self):
        for value in ("bot", "gate-main", "auto"):
            with tempfile.TemporaryDirectory() as t:
                cfg = _make(Path(t), {"b1": BASE.format(
                    bid="b1", symbols="[BTC_USDT]", label="b1",
                    extra=f"account_scope: {value}\n")})
                self.assertEqual(load_bot_config(cfg / "b1.yaml").account_scope, value)

    def test_invalid_value_raises(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[BTC_USDT]", label="b1",
                extra="account_scope: [bad]\n")})
            with self.assertRaises(GateApiError):
                load_bot_config(cfg / "b1.yaml")


class TestContractExistence(unittest.TestCase):
    """合约存在性校验是**可选/懒**的：默认不发网络请求。"""

    def _cfg(self, t: Path) -> Path:
        return _make(t, {"b1": BASE.format(
            bid="b1", symbols="[BTC_USDT, DOGE_USDT]", label="b1",
            extra="validate_contracts: true\n")}, enabled=["b1"])

    def test_off_by_default(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[BTC_USDT, NOSUCHCOIN_USDT]", label="b1", extra="")},
                enabled=["b1"])
            called = []

            def _boom(ex, env):  # pragma: no cover - 只用于断言没被调用
                called.append(ex)
                raise AssertionError("默认不该发起合约存在性校验")

            with mock.patch.object(cfgmod, "_CONTRACT_FETCHER", _boom):
                self.assertEqual(load_all_bots(cfg)["b1"].symbols,
                                 ["BTC_USDT", "NOSUCHCOIN_USDT"])
            self.assertEqual(called, [])

    def test_missing_contract_raises_when_enabled(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = self._cfg(Path(t))
            with mock.patch.object(cfgmod, "_CONTRACT_FETCHER",
                                   lambda ex, env: {"BTC_USDT"}):
                with self.assertRaises(GateApiError) as ctx:
                    load_all_bots(cfg)
            self.assertIn("DOGE_USDT", str(ctx.exception))

    def test_all_present_ok(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = self._cfg(Path(t))
            with mock.patch.object(cfgmod, "_CONTRACT_FETCHER",
                                   lambda ex, env: {"BTC_USDT", "DOGE_USDT"}):
                self.assertEqual(sorted(load_all_bots(cfg)), ["b1"])

    def test_unavailable_list_skips_check(self):
        """离线/取不到合约列表 → 跳过校验，绝不因网络失败而拦启动。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = self._cfg(Path(t))
            with mock.patch.object(cfgmod, "_CONTRACT_FETCHER", lambda ex, env: None):
                self.assertEqual(sorted(load_all_bots(cfg)), ["b1"])

    def test_env_switch_enables_check(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _make(Path(t), {"b1": BASE.format(
                bid="b1", symbols="[BTC_USDT, DOGE_USDT]", label="b1", extra="")},
                enabled=["b1"])
            with mock.patch.dict(os.environ, {"OMNIALPHA_VALIDATE_CONTRACTS": "1"}):
                with mock.patch.object(cfgmod, "_CONTRACT_FETCHER",
                                       lambda ex, env: {"BTC_USDT"}):
                    with self.assertRaises(GateApiError):
                        load_all_bots(cfg)


class TestAccountRiskConsistency(unittest.TestCase):
    """strategist 看到的 `account_risk` 必须与 executor（run 路径）完全一致（C-6）。"""

    def _setup(self, t: Path) -> Path:
        cfg = _make(t, {"b1": BASE.format(
            bid="b1", symbols="[BTC_USDT]", label="b1",
            extra="account: gate-main\naccount_risk:\n  max_total_notional_usd: 500\n")})
        (Path(t) / "config" / "accounts.yaml").write_text(
            "accounts:\n  gate-main:\n    account_risk:\n      max_total_notional_usd: 100\n",
            encoding="utf-8")
        return cfg

    def test_consistent_passes(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = self._setup(Path(t))
            bot = load_bot_config(cfg / "b1.yaml", accounts=cfgmod.load_accounts(cfg))
            assert_account_risk_consistent(bot, cfg)  # 不抛 = 通过
            self.assertEqual(bot.account_risk["max_total_notional_usd"], 100)

    def test_mismatch_raises(self):
        """bot 对象没走 accounts 合并（旧 plan 路径的形态）→ 断言必须拦下。"""
        with tempfile.TemporaryDirectory() as t:
            cfg = self._setup(Path(t))
            bot = load_bot_config(cfg / "b1.yaml")  # 故意不传 accounts
            with self.assertRaises(GateApiError) as ctx:
                assert_account_risk_consistent(bot, cfg)
            self.assertIn("account_risk", str(ctx.exception))


class TestRepoConfigsStillLoad(unittest.TestCase):
    """既有配置不得被新校验误拦（本仓 60 个基线配置，全部 enabled: false）。"""

    def test_all_repo_bots_load(self):
        bots = load_all_bots(ROOT / "config" / "bots")
        self.assertGreater(len(bots), 50)
        for bot in bots.values():
            for sym in bot.symbols:
                self.assertRegex(sym, r"^[A-Z0-9]+_[A-Z0-9]+$")

    def test_multi_symbol_bot_unchanged(self):
        bots = load_all_bots(ROOT / "config" / "bots")
        self.assertEqual(bots["ab-multi-cur"].symbols,
                         ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XAU_USDT", "XAG_USDT"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
