"""enabled 双向告警：基线显式关闭却被 overlay 打开时必须喊出来。

背景（2026-10-13 brooks-eth 线上事故复盘）：`config.py` 的结构性防护规定
**enabled 只认 overlay、基线一律忽略**（防误改基线把 bot 带上生产）。这条本身没错，
但反过来形成陷阱——用户在 git 基线里写 `enabled: false` 以为关掉了，实际 overlay
的 `enabled: true` 让 bot 全程在跑，还在管理用户手动开的仓位。原实现只在「基线写
true」时告警，反向的**静默放行**，等于把这个陷阱留着。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

BASELINE_OFF = """\
bot_id: t1
env: paper
enabled: false
symbols: ["ETH_USDT"]
label_prefix: tt
"""

BASELINE_ON = BASELINE_OFF.replace("enabled: false", "enabled: true")

BASELINE_NOKEY = """\
bot_id: t1
env: paper
symbols: ["ETH_USDT"]
label_prefix: tt
"""


def _tree(tmp: Path, baseline: str, overlay: str | None) -> Path:
    cfg = tmp / "config" / "bots"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "t1.yaml").write_text(baseline, encoding="utf-8")
    if overlay is not None:
        ov = tmp / "config" / "bots.local"
        ov.mkdir(parents=True, exist_ok=True)
        (ov / "t1.yaml").write_text(overlay, encoding="utf-8")
    return cfg / "t1.yaml"


class TestEnabledConflictWarning(unittest.TestCase):
    def test_baseline_off_overlay_on_warns(self):
        """基线 false + overlay true —— 这是事故配置，必须告警且 bot 为启用态。"""
        with tempfile.TemporaryDirectory() as d:
            p = _tree(Path(d), BASELINE_OFF, "enabled: true\n")
            with self.assertLogs("omnialpha.config", level="WARNING") as cm:
                bot = load_bot_config(p)
            self.assertTrue(bot.enabled, "overlay true 应使 bot 启用")
            joined = "\n".join(cm.output)
            self.assertIn("BOT IS ENABLED", joined)
            self.assertIn("基线里的 enabled 不生效", joined)
            # 告警必须写清真正的关闭方式，否则等于只喊疼不给药
            self.assertIn("systemd", joined)

    def test_baseline_off_no_overlay_silent(self):
        """基线 false、无 overlay → bot 关闭，且不该有噪音。"""
        with tempfile.TemporaryDirectory() as d:
            p = _tree(Path(d), BASELINE_OFF, None)
            with self.assertNoLogs("omnialpha.config", level="WARNING"):
                bot = load_bot_config(p)
            self.assertFalse(bot.enabled)

    def test_baseline_without_key_no_warning(self):
        """基线没写 enabled → 不算「显式关闭」，不告警（否则会给存量配置刷噪音）。"""
        with tempfile.TemporaryDirectory() as d:
            p = _tree(Path(d), BASELINE_NOKEY, "enabled: true\n")
            with self.assertNoLogs("omnialpha.config", level="WARNING"):
                bot = load_bot_config(p)
            self.assertTrue(bot.enabled)

    def test_baseline_on_still_warns(self):
        """既有行为保留：基线写 true 时依旧告警（只认 overlay）。"""
        with tempfile.TemporaryDirectory() as d:
            p = _tree(Path(d), BASELINE_ON, "enabled: true\n")
            with self.assertLogs("omnialpha.config", level="WARNING") as cm:
                load_bot_config(p)
            self.assertIn("IGNORED", "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
