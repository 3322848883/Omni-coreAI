"""SkillKit 多角度测试：边缘闸门 + 可见性 + 预算（自动化部分，不依赖 LLM）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.skillkit import (  # noqa: E402
    SkillRegistry,
    fit_catalog,
    render_catalog,
    run_skill_tool,
    validate_package,
    load_package,
)
from gate_bot.skillkit.models import SkillError, SkillMeta  # noqa: E402
from gate_bot.skillkit.budget import CatalogBudget  # noqa: E402

from tests._skill_fixtures import fixture_root, fixture_skills_dir  # noqa: E402

SKILLS = fixture_skills_dir()


class TestMultiSkillVisibility(unittest.TestCase):
    """多 skill 安装后的可见性矩阵。"""

    @classmethod
    def setUpClass(cls):
        cls.reg = SkillRegistry()
        cls.reg.scan([SKILLS])
        cls.ids = set(cls.reg.ids())

    def test_installed_set(self):
        # 三个测试 skill 应都在
        self.assertIn("price-action-trading", self.ids)
        self.assertIn("sentinel-risk", self.ids)
        self.assertIn("test-helper", self.ids)

    def test_model_invocation_hidden(self):
        """sentinel-risk 是 model-invocation=false → 默认可见集不含它。"""
        vis = {m.id for m in self.reg.visible_for("anybot")}
        self.assertIn("price-action-trading", vis)
        self.assertIn("test-helper", vis)
        self.assertNotIn("sentinel-risk", vis)

    def test_whitelist_filters(self):
        """bot 白名单只放行指定 skill。"""
        vis = {m.id for m in self.reg.visible_for("b", ["price-action-trading"])}
        self.assertEqual(vis, {"price-action-trading"})

    def test_empty_list_none_visible(self):
        vis = self.reg.visible_for("b", [])
        self.assertEqual(vis, [])

    def test_catalog_lists_multiple(self):
        metas = self.reg.visible_for("b")
        cat = render_catalog(metas)
        self.assertIn("price-action-trading", cat)
        self.assertIn("test-helper", cat)
        self.assertNotIn("sentinel-risk", cat)  # 用户专属不进 catalog

    def test_tool_rejects_model_invocation_false(self):
        with self.assertRaises(SkillError) as ctx:
            run_skill_tool(self.reg, {"name": "sentinel-risk"})
        self.assertIn("user-invocation-only", str(ctx.exception))

    def test_cli_can_run_model_invocation_false(self):
        """CLI run 不做 model_invocation 限制（用户显式触发）。"""
        # 直接调 load 逻辑等价于 CLI run 内部
        pkg = self.reg.get_package("sentinel-risk")
        self.assertIsNotNone(pkg)
        self.assertFalse(pkg.meta.model_invocation)


class TestCatalogBudgetDegradation(unittest.TestCase):
    """大量 skill 时 catalog 预算降级。"""

    def test_many_skills_degrade(self):
        metas = [
            SkillMeta(
                id=f"skill-{i:03d}",
                description="Use when testing skill catalog budget degradation behavior. " + "x" * 150,
                path=Path("."),
            )
            for i in range(40)
        ]
        budget = CatalogBudget(max_tokens=200, clip=80)
        kept = fit_catalog(metas, budget=budget, freq={"skill-007": 99})
        self.assertLess(len(kept), 40)
        self.assertIn("skill-007", [m.id for m in kept])  # 高频保留
        cat = render_catalog(metas, budget=budget, freq={"skill-007": 99})
        self.assertLess(len(cat), 1500)  # 被预算压住

    def test_zero_budget_one_kept(self):
        metas = [
            SkillMeta(id="a-skill", description="Use when testing.", path=Path(".")),
            SkillMeta(id="b-skill", description="Use when testing.", path=Path(".")),
        ]
        kept = fit_catalog(metas, budget=CatalogBudget(max_tokens=1))
        self.assertGreaterEqual(len(kept), 1)


class TestValidateAngles(unittest.TestCase):
    """不同角度的校验闸门。"""

    def test_model_invocation_field_parsed(self):
        pkg = load_package(SKILLS / "sentinel-risk")
        self.assertFalse(pkg.meta.model_invocation)
        pkg2 = load_package(SKILLS / "test-helper")
        self.assertTrue(pkg2.meta.model_invocation)

    def test_all_installed_validate_pass(self):
        for sid in ("price-action-trading", "sentinel-risk", "test-helper"):
            rep = validate_package(SKILLS / sid)
            self.assertTrue(rep.ok, f"{sid}: {rep.summary()}")

    def test_tool_unknown_lists_available(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        try:
            run_skill_tool(reg, {"name": "ghost-skill"})
            self.fail("should raise")
        except SkillError as e:
            self.assertIn("available", str(e))
            self.assertIn("price-action-trading", str(e))


class TestDisabledBotGates(unittest.TestCase):
    """bot 未启用 skill 时工具拒绝。"""

    def test_not_in_whitelist_rejected(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        with self.assertRaises(SkillError):
            run_skill_tool(reg, {"name": "test-helper"}, bot_id="b", enabled_ids=["price-action-trading"])

    def test_empty_whitelist_rejected(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        with self.assertRaises(SkillError):
            run_skill_tool(reg, {"name": "price-action-trading"}, bot_id="b", enabled_ids=[])


class TestRunToolWhitelistEnforcement(unittest.TestCase):
    """回归：run_tool 必须用 runner 注入的白名单，不信 LLM 传参。

    Bug 背景：曾因 enabled_skills 只从 args 取（LLM 不会传），
    导致 bot 配 skills: [] 时 LLM 仍能加载 skill。
    """

    def test_skills_empty_rejected(self):
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill", {"name": "price-action-trading"},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=[])
        self.assertIn("error", r)
        self.assertIn("not enabled", r["error"])

    def test_whitelist_allows(self):
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill", {"name": "price-action-trading"},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=["price-action-trading"])
        self.assertIn("content", r)

    def test_whitelist_other_rejected(self):
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill", {"name": "price-action-trading"},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=["test-helper"])
        self.assertIn("error", r)

    def test_none_defaults_all_visible(self):
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill", {"name": "price-action-trading"},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=None)
        self.assertIn("content", r)

    def test_llm_cannot_bypass_with_args(self):
        """LLM 在 args 里塞 enabled_skills 也不能绕过 runner 白名单。"""
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill",
                     {"name": "price-action-trading", "enabled_skills": ["price-action-trading"]},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=[])
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
