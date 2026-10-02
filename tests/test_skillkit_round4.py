"""SkillKit 第四轮：allowed-tools 运行时收窄 / 并发 / 热更新 / 长跑。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.skillkit import SkillRegistry, load_package  # noqa: E402
from omnialpha.strategist.tools import NATIVE_TOOLS  # noqa: E402

from tests._skill_fixtures import fixture_root, fixture_skills_dir  # noqa: E402

SKILLS = fixture_skills_dir()


class TestAllowedToolsRuntimeNarrowing(unittest.TestCase):
    """allowed-tools 运行时收窄逻辑。"""

    def test_meta_declares_allowed_tools(self):
        pkg = load_package(SKILLS / "narrow-tools")
        self.assertEqual(pkg.meta.allowed_tools, frozenset({"klines", "indicators", "ticker"}))

    def test_narrowing_math(self):
        """收窄后工具面 = allowed ∪ {skill, skill_ref}。"""
        allowed = {"klines", "indicators", "ticker"}
        keep = set(allowed) | {"skill", "skill_ref"}
        narrowed = [t for t in NATIVE_TOOLS if t["function"]["name"] in keep]
        names = {t["function"]["name"] for t in narrowed}
        self.assertEqual(names, {"klines", "indicators", "ticker", "skill", "skill_ref"})
        self.assertNotIn("smc_map", names)
        self.assertNotIn("orderbook", names)

    def test_skill_allowed_tools_helper(self):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig
        cfg = StrategistConfig(bot_root=fixture_root(), skills=["narrow-tools"])
        # 用未初始化实例只测 helper（不碰网络）
        runner = PlanRunner.__new__(PlanRunner)
        runner.cfg = cfg
        got = PlanRunner._skill_allowed_tools(runner, "narrow-tools")
        self.assertEqual(got, {"klines", "indicators", "ticker"})

    def test_no_allowed_tools_returns_none(self):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig
        cfg = StrategistConfig(bot_root=fixture_root(), skills=["price-action-trading"])
        runner = PlanRunner.__new__(PlanRunner)
        runner.cfg = cfg
        self.assertIsNone(PlanRunner._skill_allowed_tools(runner, "price-action-trading"))


class TestHotReload(unittest.TestCase):
    """热更新：新增 skill 后新扫描可见。"""

    def test_new_skill_visible_after_scan(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            reg = SkillRegistry()
            reg.scan([t])
            self.assertEqual(reg.ids(), [])
            # 装一个
            d = t / "hot-skill"
            d.mkdir()
            (d / "SKILL.md").write_text(
                '---\nname: hot-skill\ndescription: "Hot reload test skill. Use when testing dynamic install."\n---\n\n# Hot\n',
                encoding="utf-8",
            )
            reg2 = SkillRegistry()
            reg2.scan([t])
            self.assertEqual(reg2.ids(), ["hot-skill"])

    def test_removed_skill_gone(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = t / "gone-skill"
            d.mkdir()
            (d / "SKILL.md").write_text(
                '---\nname: gone-skill\ndescription: "Gone test skill. Use when testing removal."\n---\n\n# Gone\n',
                encoding="utf-8",
            )
            reg = SkillRegistry()
            reg.scan([t])
            self.assertIn("gone-skill", reg.ids())
            import shutil
            shutil.rmtree(d)
            reg2 = SkillRegistry()
            reg2.scan([t])
            self.assertNotIn("gone-skill", reg2.ids())


class TestConcurrentBotsIsolation(unittest.TestCase):
    """并发多 bot：不同白名单互不影响。"""

    def test_two_bots_different_whitelists(self):
        from omnialpha.strategist.tools import run_tool
        # bot A 只启用 test-helper
        ra = run_tool(None, "skill", {"name": "test-helper"},
                      bot_root=str(fixture_root()), bot_id="bot-a", skill_ids=["test-helper"])
        self.assertIn("content", ra)
        # bot A 不能加载 price-action-trading
        ra2 = run_tool(None, "skill", {"name": "price-action-trading"},
                       bot_root=str(fixture_root()), bot_id="bot-a", skill_ids=["test-helper"])
        self.assertIn("error", ra2)
        # bot B 只启用 price-action-trading
        rb = run_tool(None, "skill", {"name": "price-action-trading"},
                      bot_root=str(fixture_root()), bot_id="bot-b", skill_ids=["price-action-trading"])
        self.assertIn("content", rb)
        rb2 = run_tool(None, "skill", {"name": "test-helper"},
                       bot_root=str(fixture_root()), bot_id="bot-b", skill_ids=["price-action-trading"])
        self.assertIn("error", rb2)

    def test_catalog_isolation(self):
        from omnialpha.skillkit import render_catalog
        reg = SkillRegistry()
        reg.scan([SKILLS])
        cat_a = render_catalog(reg.visible_for("bot-a", ["test-helper"]))
        cat_b = render_catalog(reg.visible_for("bot-b", ["price-action-trading"]))
        self.assertIn("test-helper", cat_a)
        self.assertNotIn("price-action-trading", cat_a)
        self.assertIn("price-action-trading", cat_b)
        self.assertNotIn("test-helper", cat_b)


class TestJournalGrowth(unittest.TestCase):
    """长跑：journal 只追加，可解析。"""

    def test_journal_append_only_and_parsable(self):
        from omnialpha.skillkit import run_skill_tool
        reg = SkillRegistry()
        reg.scan([SKILLS])
        jp = fixture_root() / "logs" / "skill_journal.jsonl"
        before = len(jp.read_text(encoding="utf-8").strip().splitlines())
        for i in range(5):
            run_skill_tool(reg, {"name": "test-helper"}, bot_id=f"loop-{i}",
                           enabled_ids=["test-helper"], root=fixture_root())
        lines = jp.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), before + 5)
        for ln in lines[-5:]:
            rec = json.loads(ln)  # 必须可解析
            self.assertIn("ts", rec)
            self.assertIn("skill_id", rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
