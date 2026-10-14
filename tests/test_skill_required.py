"""skills_required 必载模式：catalog hint 升级（2026-10-12 brooks-eth 审计后续）。

背景：按需语义（「任务匹配时加载」）下 152 轮仅 13% 主动加载、开仓轮覆盖 3/23。
required 模式把 hint 升级为「每轮必载」，只在 bot 显式配置时生效；默认关闭、
行为与改动前完全一致（老 bot 零影响）。

**2026-10-14（N2）**：本文件此前用 `pytest.mark.skipif`，`unittest` 收集到
**0 个测试** → 只跑 unittest 的流程里静默不执行。已改写为 unittest 兼容
（`TestCase` + `unittest.skipUnless` + `self.assert*`），两种 runner 下都能跑。
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.skillkit import SkillRegistry, render_catalog  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402

REPO = ROOT
SKILLS_DIR = REPO / "skills"

# skills/ 是 gitignored 的个人内容，worktree / CI 可能缺失 → 条件跳过（unittest 原生）
_HAS_SKILL = (SKILLS_DIR / "price-action-trading" / "SKILL.md").exists()
_skip_no_skill = unittest.skipUnless(_HAS_SKILL, "skills/price-action-trading 缺失")


def _metas():
    reg = SkillRegistry()
    reg.scan([SKILLS_DIR])
    return reg.visible_for("brooks-eth", ["price-action-trading"])


@_skip_no_skill
class TestRenderCatalogRequired(unittest.TestCase):
    def test_default_hint_unchanged(self):
        """默认模式：hint 保持按需语义（向后兼容，老 bot 无感）。"""
        cat = render_catalog(_metas())
        self.assertTrue(cat.startswith("<skill_catalog>"))
        self.assertIn("任务匹配某个 skill 时", cat)
        self.assertIn("不匹配则不要加载", cat)
        self.assertNotIn("必载", cat)
        self.assertNotIn("每轮决策必须先调用", cat)

    def test_required_hint_upgraded(self):
        """required 模式：hint 升级为每轮必载 + 点名技能 + 强制清单。"""
        cat = render_catalog(_metas(), required=True)
        self.assertIn("技能目录（必载）", cat)
        self.assertIn("每轮决策必须先调用 skill(name)", cat)
        self.assertIn("price-action-trading", cat)
        self.assertIn("强制清单", cat)
        self.assertIn("流程违规", cat)
        # 免责声明保留：方法论不改风控
        self.assertIn("不改变可下单动作与风控约束", cat)

    def test_empty_metas_returns_empty_even_required(self):
        """空清单（如 skills: []）无论 required 与否都不渲染——必载不能凭空造目录。"""
        self.assertEqual(render_catalog([], required=True), "")
        self.assertEqual(render_catalog([], required=False), "")

    def test_required_lists_all_kept_skills(self):
        """required 模式点名全部可见技能 id。"""
        metas = _metas()
        cat = render_catalog(metas, required=True)
        for m in metas:
            self.assertIn(m.id, cat)


class TestStrategistConfigField(unittest.TestCase):
    def test_default_false(self):
        self.assertIs(StrategistConfig().skills_required, False)

    def test_explicit_true(self):
        self.assertIs(StrategistConfig(skills_required=True).skills_required, True)

    def test_skills_whitelist_untouched(self):
        """skills 白名单语义不变（None=全部可见, []=不可见）。"""
        self.assertIsNone(StrategistConfig().skills)
        self.assertEqual(StrategistConfig(skills=[]).skills, [])


class TestPlanRunnerCatalogIntegration(unittest.TestCase):
    """_skill_catalog 按 cfg.skills_required 切换两种 hint（集成，绕过 __init__）。"""

    def _runner(self, **kw):
        kw.setdefault("skills", ["price-action-trading"])
        cfg = StrategistConfig(bot_id="brooks-eth", bot_root=REPO, **kw)
        r = PlanRunner.__new__(PlanRunner)   # 只要 self.cfg，绕过重依赖
        r.cfg = cfg
        return r

    @_skip_no_skill
    def test_catalog_default_mode(self):
        cat = self._runner()._skill_catalog()
        self.assertIn("任务匹配某个 skill 时", cat)
        self.assertNotIn("必载", cat)

    @_skip_no_skill
    def test_catalog_required_mode(self):
        cat = self._runner(skills_required=True)._skill_catalog()
        self.assertIn("技能目录（必载）", cat)
        self.assertIn("每轮决策必须先调用 skill(name)", cat)

    def test_catalog_empty_when_no_skills(self):
        r = self._runner(skills=[])
        self.assertEqual(r._skill_catalog(), "")

    def test_catalog_empty_when_required_but_no_skills(self):
        """required=true 但 skills=[]：仍为空——必载开关不越过白名单。"""
        r = self._runner(skills=[], skills_required=True)
        self.assertEqual(r._skill_catalog(), "")


if __name__ == "__main__":
    unittest.main()
