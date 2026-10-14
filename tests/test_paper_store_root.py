# -*- coding: utf-8 -*-
"""paper 账户库必须跟随**项目根**，不能只看 CWD / 环境变量。

2026-10-14 实测缺陷：`BotConfig._create_paper_client` 解析 paper 账户库用的是
`os.environ.get("OMNIALPHA_ROOT") or Path.cwd()` + `data/bots/<id>/paper/account.db`
—— **完全不看 `--root`**。于是 `--root <tmp>` 只隔离了**配置**，账户仍写进当前
工作目录，跨次运行持仓叠加（实测同一信号跑两次：3 张 → 6 张，SL 覆盖 4/6 张）。

修法：`load_bot_config` 记住配置所在的**项目根**（`path.parent.parent`），
`_create_paper_client` 优先用它；`OMNIALPHA_ROOT` / CWD 只作兜底（直接构造
`BotConfig` 的调用方行为不变）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import BotConfig, load_bot_config  # noqa: E402

_BOT_YAML = """\
bot_id: {bot}
env: paper
symbols: [BTC_USDT]
label_prefix: pr
paper:
  feed_exchange: gate
  initial_capital: 10000
  leverage: 10
"""


def _write_cfg(root: Path, bot: str = "pr-bot", extra: str = "") -> Path:
    d = root / "config" / "bots"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{bot}.yaml"
    p.write_text(_BOT_YAML.format(bot=bot) + extra, encoding="utf-8")
    return p


class TestPaperStoreFollowsProjectRoot(unittest.TestCase):
    def test_load_bot_config_records_project_root(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            bot = load_bot_config(_write_cfg(root))
            self.assertEqual(Path(bot.root).resolve(), root.resolve())

    def test_paper_store_under_project_root(self):
        """核心：`--root <tmp>` 必须把账户库也放进 <tmp>。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            bot = load_bot_config(_write_cfg(root))
            client = bot.create_client()
            want = (root / "data" / "bots" / bot.bot_id / "paper" / "account.db")
            self.assertEqual(Path(client.store.path).resolve(), want.resolve())

    def test_two_roots_do_not_share_the_account(self):
        """回归护栏：两个不同 root 下同名 bot 的账户库必须不同。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as a, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as b:
            c1 = load_bot_config(_write_cfg(Path(a))).create_client()
            c2 = load_bot_config(_write_cfg(Path(b))).create_client()
            self.assertNotEqual(Path(c1.store.path).resolve(),
                                Path(c2.store.path).resolve())

    def test_paper_alert_store_under_project_root(self):
        """paper 引擎自己的 AlertStore 也必须落在项目根（原先 off-by-one 落进 data/data）。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            bot = load_bot_config(_write_cfg(root))
            client = bot.create_client()
            self.assertIsNotNone(client.engine.alert_store)
            want = root / "data" / "bots" / bot.bot_id / "state" / "alerts.json"
            self.assertEqual(Path(client.engine.alert_store.path).resolve(), want.resolve())

    def test_explicit_store_path_still_wins(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            custom = root / "custom" / "acct.db"
            bot = load_bot_config(
                _write_cfg(root, extra=f"  store_path: {custom.as_posix()}\n"))
            self.assertEqual(Path(bot.create_client().store.path).resolve(),
                             custom.resolve())

    def test_env_is_overridden_by_project_root(self):
        """`--root` 比 OMNIALPHA_ROOT 更权威（与 `_root_from_args` 的优先级一致）。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as other:
            old = os.environ.get("OMNIALPHA_ROOT")
            os.environ["OMNIALPHA_ROOT"] = other
            try:
                root = Path(td)
                bot = load_bot_config(_write_cfg(root))
                client = bot.create_client()
                self.assertEqual(
                    Path(client.store.path).resolve(),
                    (root / "data" / "bots" / bot.bot_id / "paper" / "account.db").resolve())
            finally:
                if old is None:
                    os.environ.pop("OMNIALPHA_ROOT", None)
                else:
                    os.environ["OMNIALPHA_ROOT"] = old

    def test_direct_botconfig_falls_back_to_env(self):
        """直接构造的 BotConfig（无 root）→ 回退 OMNIALPHA_ROOT（行为不变）。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            old = os.environ.get("OMNIALPHA_ROOT")
            os.environ["OMNIALPHA_ROOT"] = td
            try:
                bot = BotConfig(bot_id="env-bot", env="paper", symbols=["BTC_USDT"])
                client = bot.create_client()
                self.assertEqual(
                    Path(client.store.path).resolve(),
                    (Path(td) / "data" / "bots" / "env-bot" / "paper" / "account.db").resolve())
            finally:
                if old is None:
                    os.environ.pop("OMNIALPHA_ROOT", None)
                else:
                    os.environ["OMNIALPHA_ROOT"] = old


if __name__ == "__main__":
    unittest.main()
