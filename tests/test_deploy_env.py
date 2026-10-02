"""环境区分部署测试：overlay 合并 / 乱码还原 / deploy-check。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import deep_merge, load_all_bots, load_bot_config, overlay_dir_for  # noqa: E402
from omnialpha.skillkit.cli import demojibake  # noqa: E402

BASE_YAML = """\
bot_id: {bid}
enabled: false
env: paper
symbols: [BTC_USDT]
strategist:
  timeframe: 15m
  risk:
    min_confidence: 0.7
    max_chips: 1
"""


class TestDeepMerge(unittest.TestCase):
    def test_dict_recursive(self):
        b = {"a": 1, "n": {"x": 1, "y": 2}}
        o = {"n": {"y": 9, "z": 3}}
        self.assertEqual(deep_merge(b, o), {"a": 1, "n": {"x": 1, "y": 9, "z": 3}})

    def test_list_replaced(self):
        self.assertEqual(deep_merge({"l": [1, 2]}, {"l": [9]}), {"l": [9]})

    def test_scalar_replaced(self):
        self.assertEqual(deep_merge({"a": 1}, {"a": 2}), {"a": 2})

    def test_null_deletes_key(self):
        self.assertEqual(deep_merge({"a": 1, "b": 2}, {"a": None}), {"b": 2})

    def test_base_not_mutated(self):
        b = {"n": {"x": 1}}
        deep_merge(b, {"n": {"y": 2}})
        self.assertEqual(b, {"n": {"x": 1}})

    def test_empty_overlay(self):
        b = {"a": 1}
        self.assertEqual(deep_merge(b, {}), b)
        self.assertEqual(deep_merge(b, None), b)


class TestOverlayLoading(unittest.TestCase):
    def _setup(self, td: Path):
        cfg = td / "config" / "bots"
        cfg.mkdir(parents=True)
        (cfg / "bot-a.yaml").write_text(BASE_YAML.format(bid="bot-a"), encoding="utf-8")
        (cfg / "bot-b.yaml").write_text(BASE_YAML.format(bid="bot-b"), encoding="utf-8")
        return cfg

    def test_overlay_dir_derivation(self):
        self.assertEqual(overlay_dir_for(Path("/x/config/bots")), Path("/x/config/bots.local"))

    def test_no_overlay_baseline_used(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = self._setup(Path(t))
            bots = load_all_bots(cfg)
            self.assertFalse(bots["bot-a"].enabled)
            self.assertEqual(bots["bot-a"].strategist["timeframe"], "15m")

    def test_overlay_enables_and_overrides(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cfg = self._setup(td)
            ov = td / "config" / "bots.local"
            ov.mkdir()
            (ov / "bot-a.yaml").write_text(
                "enabled: true\nstrategist:\n  timeframe: 1h\n  risk:\n    min_confidence: 0.9\n",
                encoding="utf-8",
            )
            bots = load_all_bots(cfg)
            self.assertTrue(bots["bot-a"].enabled)
            self.assertEqual(bots["bot-a"].strategist["timeframe"], "1h")
            self.assertEqual(bots["bot-a"].strategist["risk"]["min_confidence"], 0.9)
            # 未覆盖的 bot 保持基线
            self.assertFalse(bots["bot-b"].enabled)

    def test_overlay_only_affects_named_bot(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cfg = self._setup(td)
            ov = td / "config" / "bots.local"
            ov.mkdir()
            (ov / "bot-a.yaml").write_text("enabled: true\n", encoding="utf-8")
            b = load_bot_config(cfg / "bot-b.yaml")
            self.assertFalse(b.enabled)

    def test_explicit_overlay_dir(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cfg = self._setup(td)
            other = td / "elsewhere"
            other.mkdir()
            (other / "bot-a.yaml").write_text("enabled: true\n", encoding="utf-8")
            bots = load_all_bots(cfg, overlay_dir=other)
            self.assertTrue(bots["bot-a"].enabled)

    # ── 结构性防护：基线 enabled 一律忽略 ──
    def test_baseline_enabled_true_is_ignored(self):
        """基线写 enabled: true 不生效（防误改基线把 bot 带上生产）。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cfg = self._setup(td)
            # 直接改基线为 true
            (cfg / "bot-a.yaml").write_text(
                BASE_YAML.format(bid="bot-a").replace("enabled: false", "enabled: true"),
                encoding="utf-8",
            )
            bots = load_all_bots(cfg)
            self.assertFalse(bots["bot-a"].enabled, "基线 enabled:true 必须被忽略")

    def test_baseline_enabled_true_with_overlay_false_stays_false(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cfg = self._setup(td)
            (cfg / "bot-a.yaml").write_text(
                BASE_YAML.format(bid="bot-a").replace("enabled: false", "enabled: true"),
                encoding="utf-8",
            )
            ov = td / "config" / "bots.local"
            ov.mkdir()
            (ov / "bot-a.yaml").write_text("enabled: false\n", encoding="utf-8")
            bots = load_all_bots(cfg)
            self.assertFalse(bots["bot-a"].enabled)

    def test_only_overlay_can_enable(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cfg = self._setup(td)
            (cfg / "bot-a.yaml").write_text(
                BASE_YAML.format(bid="bot-a").replace("enabled: false", "enabled: true"),
                encoding="utf-8",
            )
            ov = td / "config" / "bots.local"
            ov.mkdir()
            (ov / "bot-a.yaml").write_text("enabled: true\n", encoding="utf-8")
            bots = load_all_bots(cfg)
            self.assertTrue(bots["bot-a"].enabled, "只有 overlay 能启用")


class TestDemojibake(unittest.TestCase):
    def test_cp866_recovery(self):
        good = "V1_趋势篇"
        moji = good.encode("utf-8").decode("cp866")
        self.assertNotEqual(moji, good)
        self.assertEqual(demojibake(moji), good)

    def test_ascii_untouched(self):
        self.assertIsNone(demojibake("references"))

    def test_normal_chinese_untouched(self):
        # 正常中文名不应被误判（cp866 编码会失败）
        self.assertIsNone(demojibake("趋势篇"))

    def test_various_names(self):
        for good in ("V2_区间", "V3_反转", "测试目录"):
            moji = good.encode("utf-8").decode("cp866")
            self.assertEqual(demojibake(moji), good)


class TestDeployCheckCommand(unittest.TestCase):
    def test_deploy_check_runs(self):
        r = subprocess.run(
            [sys.executable, "-m", "omnialpha", "deploy-check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=120,
        )
        out = r.stdout + r.stderr
        self.assertIn("部署检查", out)
        self.assertIn("配置 overlay", out)
        self.assertIn("已装 skill", out)
        self.assertIn("结论:", out)


class TestSkillDoctorCommand(unittest.TestCase):
    def test_doctor_runs(self):
        r = subprocess.run(
            [sys.executable, "-m", "omnialpha", "skill", "doctor"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=120,
        )
        out = r.stdout + r.stderr
        self.assertIn("体检完成", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
