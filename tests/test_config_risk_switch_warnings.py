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


if __name__ == "__main__":
    unittest.main()
