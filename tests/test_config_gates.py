# -*- coding: utf-8 -*-
"""配置闸门与「配了但不生效」防护。

2026-10-27 实测漏洞：`config_keys.TOP_LEVEL` 白名单里有
`manage_positions` / `allow_close` / `allow_reduce`，但 `BotConfig` **没有对应
字段**；消费方是 `watcher` 的 `getattr(bot, "manage_positions", True)` ——
属性不存在 ⇒ 恒取默认 True。于是 yaml 写 `manage_positions: false` 被静默忽略：
bot 照样管仓、照样平用户手动仓，且因为键在白名单里，连 typo 告警都没有。

本文件钉住四条不变量：
  1. 三个闸门键 yaml → BotConfig 真生效（缺省仍为 True）；
  2. BotConfig 的**每个字段**都必须在 `TOP_LEVEL` 白名单里（防同类漏洞复发）；
  3. 代码**真在消费**的键必须在白名单里（白名单过期的反向保护）；
  4. 未知键 → 告警但仍能加载（阶段 1：不报错，逼人绕过校验更糟）。
"""
from __future__ import annotations

import dataclasses
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha import config_keys  # noqa: E402
from omnialpha.config import BotConfig, load_bot_config  # noqa: E402


def _write(cfg_text: str, overlay_text: str = "") -> Path:
    """写一个临时 bot 配置（可选 overlay），返回基线路径。"""
    td = tempfile.mkdtemp()
    bots = Path(td) / "bots"
    local = Path(td) / "bots.local"
    bots.mkdir()
    local.mkdir()
    p = bots / "t.yaml"
    p.write_text(textwrap.dedent(cfg_text), encoding="utf-8")
    if overlay_text:
        (local / "t.yaml").write_text(textwrap.dedent(overlay_text), encoding="utf-8")
    return p


BASE = """
    bot_id: t
    env: paper
    symbols: [BTC_USDT]
    label_prefix: t
"""


class TestManagementGatesFromYaml(unittest.TestCase):
    def test_defaults_are_true(self):
        c = load_bot_config(_write(BASE))
        self.assertTrue(c.manage_positions)
        self.assertTrue(c.allow_close)
        self.assertTrue(c.allow_reduce)

    def test_manage_positions_false_is_honored(self):
        c = load_bot_config(_write(BASE + "    manage_positions: false\n"))
        self.assertFalse(c.manage_positions,
                         "yaml 写 false 必须真生效（此前被 getattr 默认值吞掉）")

    def test_allow_close_false_is_honored(self):
        c = load_bot_config(_write(BASE + "    allow_close: false\n"))
        self.assertFalse(c.allow_close)

    def test_allow_reduce_false_is_honored(self):
        c = load_bot_config(_write(BASE + "    allow_reduce: false\n"))
        self.assertFalse(c.allow_reduce)

    def test_overlay_can_turn_gate_off(self):
        """overlay 关闸（生产里就是这么改的：基线不动，只改 bots.local）。"""
        c = load_bot_config(_write(BASE, "    manage_positions: false\n"))
        self.assertFalse(c.manage_positions)


class TestExecutorHonorsGates(unittest.TestCase):
    """BotConfig → Executor 的接线：闸门真的挡住动作，且是良性 skip。"""

    class _Gate:
        env = "live"

        def banner(self):
            return ""

        def get_contract(self, symbol):
            class _M:
                quanto_multiplier = 0.0001
                order_price_round = 0.1
                price_tick = 0.1
                size_min = 1
                order_size_round = 1
            return _M()

        def get_position_mode(self):
            return "dual"

        def get_positions(self):
            return []

        def list_price_orders(self, symbol=None):
            return []

        def list_orders(self, contract=None):
            return []

    def _ex(self, **kw):
        from omnialpha.executor import Executor
        td = tempfile.mkdtemp()
        return Executor(self._Gate(), root=Path(td), bot_id="t",
                        label_prefix="t", **kw)

    def test_manage_positions_off_blocks_close(self):
        ex = self._ex(manage_positions=False)
        gate = ex._management_gate("close", None)
        self.assertIn("MANAGE_POSITIONS_OFF", gate)

    def test_allow_close_off_blocks_close_but_not_open(self):
        ex = self._ex(allow_close=False)
        self.assertIn("CLOSE_DISABLED", ex._management_gate("close", None))
        self.assertEqual(ex._management_gate("open_long", None), "")

    def test_allow_reduce_off_blocks_reduce_but_not_close(self):
        ex = self._ex(allow_reduce=False)
        self.assertIn("REDUCE_DISABLED", ex._management_gate("reduce", None))
        self.assertEqual(ex._management_gate("close", None), "")


class TestWhitelistCoversDataclass(unittest.TestCase):
    """每个 BotConfig 字段都必须在 TOP_LEVEL 白名单里。

    这条守的是「字段存在但白名单没有 → 配置校验报未知键」与反方向的
    「白名单有但字段没有 → 写了不生效」。两侧都必须对得上。
    """

    def test_every_field_whitelisted(self):
        fields = {f.name for f in dataclasses.fields(BotConfig)}
        missing = sorted(fields - set(config_keys.TOP_LEVEL))
        self.assertEqual(missing, [], f"BotConfig 字段未进白名单: {missing}")


class TestWhitelistCoversConsumedKeys(unittest.TestCase):
    """代码真在消费的键不能因为白名单过期而被报成「未知」。"""

    def test_sl_move_policy(self):
        self.assertIn("sl_move_policy", config_keys.ACCOUNT_RISK)

    def test_runtime_and_require_checks(self):
        self.assertIn("runtime", config_keys.STRATEGIST)
        self.assertIn("require_checks", config_keys.STRATEGIST)

    def test_structure(self):
        self.assertIn("structure", config_keys.STRATEGIST_MARKET)

    def test_required_coverage(self):
        self.assertIn("required_coverage", config_keys.STRATEGIST_TOOLS)


class TestUnknownKeyWarnsButLoads(unittest.TestCase):
    def test_unknown_top_level_key_warns(self):
        p = _write(BASE + "    manage_position: false\n")  # typo：少个 s
        with self.assertLogs("omnialpha.config", level="WARNING") as cm:
            c = load_bot_config(p)
        joined = "\n".join(cm.output)
        self.assertIn("manage_position", joined, joined)
        self.assertTrue(c.manage_positions, "typo 不能悄悄关掉闸门")

    def test_unknown_account_risk_key_warns(self):
        p = _write(BASE + """
    account_risk:
      auto_protect: live
      auto_protect_sl_pct: 2.0
      auto_protectt: live
""")
        with self.assertLogs("omnialpha.config", level="WARNING") as cm:
            load_bot_config(p)
        self.assertIn("account_risk.auto_protectt", "\n".join(cm.output))

    def test_clean_config_no_unknown_warning(self):
        p = _write(BASE + """
    account_risk:
      auto_protect: live
      auto_protect_sl_pct: 2.0
      sl_move_policy: strict
    strategist:
      runtime:
        plan_loop: true
""")
        import logging
        logger = logging.getLogger("omnialpha.config")
        records: list[str] = []

        class H(logging.Handler):
            def emit(self, r):
                if "未识别" in r.getMessage():
                    records.append(r.getMessage())

        h = H()
        logger.addHandler(h)
        try:
            load_bot_config(p)
        finally:
            logger.removeHandler(h)
        self.assertEqual(records, [], f"合法配置不该报未知键: {records}")


class TestNoDeadKeysInConfigs(unittest.TestCase):
    """仓库里的 bot 配置不许再出现**已核实零消费**的键。

    2026-10-27 删除的三个键都带着「承诺了具体行为」的注释（fail-closed 拒绝启动 /
    重挂入场单前撤旧单 / 首轮强制载技能），实际全仓零读取点 —— 这种「写了但不生效」
    比没写更危险：读配置的人会以为保护已经开着。

    真需要对应能力时的正确入口（已写在各 yaml 的注释里）：
      · 替换入场单 → `default_replace: none|symbol|all` 或信号里的 `replace`
      · 人格失配 → 目前只有 warn（`_warn_persona_symbols`），fail-closed 未实现
      · 首轮强制载技能 → 需在 loop 里用 tool_choice 强制，没有配置键
    """

    DEAD = ("persona_strict", "replace_on_entry", "require_first")

    def test_no_dead_keys(self):
        import yaml

        hits: list[str] = []
        for p in sorted((ROOT / "config" / "bots").glob("*.yaml")):
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            for section in ("strategist", "account_risk", ""):
                node = data.get(section) if section else data
                if not isinstance(node, dict):
                    continue
                for k in self.DEAD:
                    if k in node:
                        hits.append(f"{p.name}: {section or '<root>'}.{k}")
                tools = node.get("tools")
                if isinstance(tools, dict):
                    for k in self.DEAD:
                        if k in tools:
                            hits.append(f"{p.name}: {section or '<root>'}.tools.{k}")
        self.assertEqual(hits, [], f"死键又被写回来了: {hits}")


if __name__ == "__main__":
    unittest.main()
