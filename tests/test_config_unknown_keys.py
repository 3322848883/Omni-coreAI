# -*- coding: utf-8 -*-
"""未知配置键校验（N1）+ 配置键文档漂移（N13）。

背景：`account_risk` / `strategist` / `paper` 等段是自由 dict，读不到的键**静默 no-op**。
实测先例：5 个 `ab-*` bot 配了 `auto_protect: "dry"` 却没配 `auto_protect_sl_pct`，
`ensure_protection` 在 `pct<=0` 时早退 → 「dry 观察期」什么都没观察到。键名 typo
（`enabld` / `prompt_fil`）此前连一条日志都没有。

阶段 1 = **只告警不报错**（存量配置里可能有「写了但没人读」的键，一律报错会逼人绕过校验）。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_all_bots, load_bot_config, overlay_dir_for  # noqa: E402
from omnialpha.config_keys import (  # noqa: E402
    ACCOUNT_ENTRY,
    SCHEMA,
    find_unknown_keys,
)

CONFIG_DIR = ROOT / "config" / "bots"


class TestUnknownKeyDetection(unittest.TestCase):
    def test_typo_caught_at_top_level(self):
        self.assertEqual(find_unknown_keys({"enabld": True}), ["enabld"])

    def test_typo_caught_in_nested_section(self):
        got = find_unknown_keys({"account_risk": {"auto_protect_sl_pctt": 2.0}})
        self.assertEqual(got, ["account_risk.auto_protect_sl_pctt"])

    def test_typo_caught_in_strategist(self):
        got = find_unknown_keys({"strategist": {"prompt_fil": "x.md"}})
        self.assertEqual(got, ["strategist.prompt_fil"])

    def test_known_keys_are_silent(self):
        data = {
            "bot_id": "x", "enabled": True, "env": "paper", "symbols": ["BTC_USDT"],
            "account_risk": {"auto_protect": "dry", "auto_protect_sl_pct": 2.0},
            "strategist": {"prompt_file": "p.md", "tools": {"enabled": True}},
            "paper": {"leverage": 10},
        }
        self.assertEqual(find_unknown_keys(data), [])

    def test_children_of_unlisted_path_are_not_double_flagged(self):
        """未纳入白名单的嵌套层：只报父键，不下钻（避免一条错配刷出一堆子键）。"""
        got = find_unknown_keys({"strategist": {"weird": {"x": 1, "y": 2}}})
        self.assertEqual(got, ["strategist.weird"])

    def test_all_real_bots_have_no_unknown_keys(self):
        """存量 60 个 bot 必须零未知键 —— 否则白名单不完整（会有误报噪音）。"""
        bots = load_all_bots(CONFIG_DIR, overlay_dir_for(CONFIG_DIR))
        self.assertGreater(len(bots), 0)
        for path in sorted(CONFIG_DIR.glob("*.yaml")):
            if path.name.startswith("_"):
                continue
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            self.assertEqual(find_unknown_keys(data), [], f"{path.name} 有未知键")

    def test_load_warns_on_typo(self):
        from unittest import mock
        from omnialpha import config as cfgmod
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t-bot.yaml"
            p.write_text("bot_id: t-bot\nenv: paper\nsymbols: [BTC_USDT]\n"
                         "label_prefix: t\nenabld: true\n", encoding="utf-8")
            with mock.patch.object(cfgmod.log, "warning") as w:
                load_bot_config(p)
            msgs = [str(c.args) for c in w.call_args_list]
            self.assertTrue(any("未知配置键" in m for m in msgs), msgs)

    def test_risk_cap_keys_are_whitelisted(self):
        """账户级合并的「越小越严」字段必须都在白名单里。

        这是防「代码加了新的合并键、白名单没跟上」的护栏 —— 漏了会变成
        「账户级配了却不生效」的静默失效（本仓最贵的失败形态）。
        """
        from omnialpha.config import _RISK_CAP_KEYS
        missing = [k for k in _RISK_CAP_KEYS if k not in SCHEMA["account_risk"]]
        self.assertEqual(missing, [], f"这些合并键不在白名单：{missing}")


class TestConfigExamples(unittest.TestCase):
    """示例配置必须存在且与白名单一致（N12）。

    本机没有 `config/accounts.yaml` / `alerts.yaml` 是**设计使然** —— 它们含密钥、
    与环境相关，不入 git（`load_accounts` 缺文件即返回 `{}`，行为不变）。但**示例**
    必须存在、可解析、且不能腐化（示例里的键跑出白名单 = 文档与代码脱节）。
    """

    def _load(self, name: str) -> dict:
        import yaml
        p = ROOT / "config" / name
        self.assertTrue(p.is_file(), f"缺示例文件 config/{name}")
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    def test_accounts_example_exists(self):
        d = self._load("_accounts.example.yaml")
        self.assertIn("accounts", d)
        self.assertTrue(d["accounts"], "示例里至少应有一个账户条目")

    def test_accounts_example_entry_keys_whitelisted(self):
        d = self._load("_accounts.example.yaml")
        for name, entry in (d.get("accounts") or {}).items():
            with self.subTest(account=name):
                unknown = [k for k in entry if k not in ACCOUNT_ENTRY]
                self.assertEqual(unknown, [], f"{name} 有白名单外的键")

    def test_accounts_example_risk_keys_whitelisted(self):
        d = self._load("_accounts.example.yaml")
        for name, entry in (d.get("accounts") or {}).items():
            with self.subTest(account=name):
                ar = entry.get("account_risk") or {}
                unknown = [k for k in ar if k not in SCHEMA["account_risk"]]
                self.assertEqual(unknown, [], f"{name}.account_risk 有白名单外的键")

    def test_missing_accounts_file_is_tolerated(self):
        """真文件缺失时 `load_accounts` 必须静默返回 {}（不是报错）。"""
        from omnialpha.config import load_accounts
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_accounts(Path(td)), {})


class TestConfigReferenceFresh(unittest.TestCase):
    """文档漂移检测：`docs/config-reference.md` 必须等于白名单的渲染结果。"""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "gen_config_reference", ROOT / "scripts" / "gen_config_reference.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_doc_exists(self):
        self.assertTrue((ROOT / "docs" / "config-reference.md").is_file(),
                        "缺 docs/config-reference.md，请跑 scripts/gen_config_reference.py")

    def test_doc_matches_whitelist(self):
        doc = ROOT / "docs" / "config-reference.md"
        self.assertEqual(
            doc.read_text(encoding="utf-8"), self.mod.render(),
            "配置键文档与白名单漂移 —— 重跑 python scripts/gen_config_reference.py")

    def test_render_is_deterministic(self):
        self.assertEqual(self.mod.render(), self.mod.render())

    def test_every_whitelist_path_has_a_description(self):
        for path in SCHEMA:
            with self.subTest(path=path):
                from omnialpha.config_keys import DESCRIPTIONS
                self.assertIn(path, DESCRIPTIONS, f"{path} 缺说明（文档会不完整）")


if __name__ == "__main__":
    unittest.main()
