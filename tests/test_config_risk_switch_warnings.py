# -*- coding: utf-8 -*-
"""风控开关「静默失效」组合的加载期告警（2026-10-14 全面检查发现）。

实测 `ab-*` 5 个 bot 配了 `account_risk.auto_protect: "dry"` 却没有
`auto_protect_sl_pct` —— 而 `ensure_protection` 在 `pct <= 0` 时**直接早退**，
连 dry 模式下「本来会挂什么」都不计算。于是「dry 观察期」其实什么都没观察到，
这正是本仓反复出现的「静默失效」形态（同类先例：enabled 基线/overlay 冲突告警）。

只告警不报错：改不改是策略决定，但必须让人看得见。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

_BASE = """\
bot_id: {bot_id}
enabled: true
env: paper
symbols: [BTC_USDT]
label_prefix: {prefix}
"""


def _write(td: str, extra: str, *, prefix: str = "zz", bot_id: str = "zz-bot") -> Path:
    p = Path(td) / f"{bot_id}.yaml"
    p.write_text(_BASE.format(bot_id=bot_id, prefix=prefix) + extra, encoding="utf-8")
    return p


class TestRiskSwitchWarnings(unittest.TestCase):
    def test_dry_without_pct_warns(self):
        """auto_protect=dry 但没给 pct → 完全不生效，必须告警。"""
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, "account_risk:\n  auto_protect: \"dry\"\n")
            with self.assertLogs("omnialpha.config", level="WARNING") as cm:
                load_bot_config(p)
            self.assertTrue(any("auto_protect_sl_pct" in m for m in cm.output), cm.output)

    def test_dry_with_pct_does_not_warn(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, "account_risk:\n  auto_protect: \"dry\"\n"
                           "  auto_protect_sl_pct: 2.0\n")
            with self.assertLogs("omnialpha.config", level="WARNING") as cm:
                load_bot_config(p)
            self.assertFalse(any("auto_protect_sl_pct" in m for m in cm.output), cm.output)

    def test_peak_trail_without_atr_warns(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, "account_risk:\n  peak_trail: true\n")
            with self.assertLogs("omnialpha.config", level="WARNING") as cm:
                load_bot_config(p)
            self.assertTrue(any("peak_trail_atr" in m for m in cm.output), cm.output)

    def test_manage_positions_false_with_risk_switches_warns(self):
        """只开仓不管仓，却配了三条程序路径 → 总闸会全部熄火（白配）。"""
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, "manage_positions: false\n"
                           "account_risk:\n"
                           "  auto_protect: true\n"
                           "  auto_protect_sl_pct: 2.0\n"
                           "  give_back: true\n")
            with self.assertLogs("omnialpha.config", level="WARNING") as cm:
                load_bot_config(p)
            self.assertTrue(any("manage_positions=false" in m for m in cm.output), cm.output)

    def test_clean_config_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, "account_risk:\n  auto_protect: \"dry\"\n"
                           "  auto_protect_sl_pct: 2.0\n"
                           "  peak_trail: true\n"
                           "  peak_trail_atr: 2.0\n"
                           "  give_back: true\n")
            with self.assertLogs("omnialpha.config", level="WARNING") as cm:
                load_bot_config(p)
            self.assertEqual([m for m in cm.output if "account_risk" in m], [])


class TestSkillVisibilityWarning(unittest.TestCase):
    """技能目录默认全量可见 = 跨 bot 影响系统提示词的路径（2026-10-14 提示词层隔离）。

    `strategist.skills` 不写时 `SkillRegistry.visible_for(bot, None)` 返回**全部**
    model-invocable 技能 —— 而技能目录是 system prompt 的一部分。于是「往全局技能库
    加一个技能」会悄悄改到所有未声明 skills 的 bot。加载期一次性告警让它可见。
    """

    def setUp(self):
        from omnialpha import config as cfgmod
        self.cfgmod = cfgmod
        self._old = cfgmod._SKILL_VIS_WARNED
        cfgmod._SKILL_VIS_WARNED = False
        cfgmod._SKILL_LIB_SCANNED.clear()

    def tearDown(self):
        self.cfgmod._SKILL_VIS_WARNED = self._old
        self.cfgmod._SKILL_LIB_SCANNED.clear()

    def _load(self, p: Path) -> list[str]:
        """加载配置并返回本轮的 warning 文案（不要求一定有日志）。"""
        from unittest import mock
        with mock.patch.object(self.cfgmod.log, "warning") as w:
            load_bot_config(p)
        out = []
        for c in w.call_args_list:
            try:
                out.append(str(c.args[0]) % tuple(c.args[1:]))
            except (TypeError, ValueError):
                out.append(str(c.args))
        return out

    def _project(self, td: str, *, with_skill: bool, bot_id: str = "sv-bot",
                 overlay: bool = True, extra: str = "") -> Path:
        root = Path(td)
        (root / "config" / "bots").mkdir(parents=True, exist_ok=True)
        if with_skill:
            d = root / "skills" / "demo"
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text("---\nid: demo\ndescription: x\n---\n",
                                        encoding="utf-8")
        p = root / "config" / "bots" / f"{bot_id}.yaml"
        p.write_text(f"bot_id: {bot_id}\nenv: paper\nsymbols: [BTC_USDT]\n"
                     f"label_prefix: {bot_id[:4]}\n" + extra, encoding="utf-8")
        if overlay:
            od = root / "config" / "bots.local"
            od.mkdir(parents=True, exist_ok=True)
            (od / f"{bot_id}.yaml").write_text("enabled: true\n", encoding="utf-8")
        return p

    def test_undeclared_skills_warns_when_library_nonempty(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._project(td, with_skill=True)
            msgs = self._load(p)
            self.assertTrue(any("默认全量可见" in m for m in msgs), msgs)

    def test_declared_skills_does_not_warn(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._project(td, with_skill=True,
                              extra="strategist:\n  skills: []\n")
            msgs = self._load(p)
            self.assertFalse(any("默认全量可见" in m for m in msgs), msgs)

    def test_empty_library_does_not_warn(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._project(td, with_skill=False)
            msgs = self._load(p)
            self.assertFalse(any("默认全量可见" in m for m in msgs), msgs)

    def test_disabled_bot_does_not_warn(self):
        """未启用的 bot 不告警（否则本地 60 个未启用 bot 会刷屏）。"""
        with tempfile.TemporaryDirectory() as td:
            p = self._project(td, with_skill=True, overlay=False)
            msgs = self._load(p)
            self.assertFalse(any("默认全量可见" in m for m in msgs), msgs)

    def test_warns_only_once_per_process(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config" / "bots").mkdir(parents=True)
            (root / "config" / "bots.local").mkdir(parents=True)
            d = root / "skills" / "demo"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("x", encoding="utf-8")
            for name, want in (("aaa-bot", 1), ("bbb-bot", 0)):
                p = root / "config" / "bots" / f"{name}.yaml"
                p.write_text(f"bot_id: {name}\nenv: paper\nsymbols: [BTC_USDT]\n"
                             f"label_prefix: {name[:4]}\n", encoding="utf-8")
                (root / "config" / "bots.local" / f"{name}.yaml").write_text(
                    "enabled: true\n", encoding="utf-8")
                msgs = self._load(p)
                n = sum(1 for m in msgs if "默认全量可见" in m)
                self.assertEqual(n, want, f"{name}: {msgs}")


if __name__ == "__main__":
    unittest.main()
