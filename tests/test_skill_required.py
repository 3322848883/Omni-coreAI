"""skills_required 必载模式：catalog hint 升级（2026-10-12 brooks-eth 审计后续）。

背景：按需语义（「任务匹配时加载」）下 152 轮仅 13% 主动加载、开仓轮覆盖 3/23。
required 模式把 hint 升级为「每轮必载」，只在 bot 显式配置时生效；默认关闭、
行为与改动前完全一致（老 bot 零影响）。
"""
from pathlib import Path

import pytest

from omnialpha.skillkit import SkillRegistry, render_catalog
from omnialpha.strategist.loop import StrategistConfig

REPO = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO / "skills"


def _metas():
    reg = SkillRegistry()
    reg.scan([SKILLS_DIR])
    return reg.visible_for("brooks-eth", ["price-action-trading"])


@pytest.mark.skipif(not (SKILLS_DIR / "price-action-trading" / "SKILL.md").exists(),
                    reason="skills/price-action-trading 是 gitignored 个人内容，worktree 可能缺失")
class TestRenderCatalogRequired:
    def test_default_hint_unchanged(self):
        """默认模式：hint 保持按需语义（向后兼容，老 bot 无感）。"""
        cat = render_catalog(_metas())
        assert cat.startswith("<skill_catalog>")
        assert "任务匹配某个 skill 时" in cat
        assert "不匹配则不要加载" in cat
        assert "必载" not in cat
        assert "每轮决策必须先调用" not in cat

    def test_required_hint_upgraded(self):
        """required 模式：hint 升级为每轮必载 + 点名技能 + 强制清单。"""
        cat = render_catalog(_metas(), required=True)
        assert "技能目录（必载）" in cat
        assert "每轮决策必须先调用 skill(name)" in cat
        assert "price-action-trading" in cat
        assert "强制清单" in cat
        assert "流程违规" in cat
        # 免责声明保留：方法论不改风控
        assert "不改变可下单动作与风控约束" in cat

    def test_empty_metas_returns_empty_even_required(self):
        """空清单（如 skills: []）无论 required 与否都不渲染——必载不能凭空造目录。"""
        assert render_catalog([], required=True) == ""
        assert render_catalog([], required=False) == ""

    def test_required_lists_all_kept_skills(self):
        """required 模式点名全部可见技能 id。"""
        metas = _metas()
        cat = render_catalog(metas, required=True)
        for m in metas:
            assert m.id in cat


class TestStrategistConfigField:
    def test_default_false(self):
        cfg = StrategistConfig()
        assert cfg.skills_required is False

    def test_explicit_true(self):
        cfg = StrategistConfig(skills_required=True)
        assert cfg.skills_required is True

    def test_skills_whitelist_untouched(self):
        """skills 白名单语义不变（None=全部可见, []=不可见）。"""
        assert StrategistConfig().skills is None
        assert StrategistConfig(skills=[]).skills == []


class TestPlanRunnerCatalogIntegration:
    """_skill_catalog 按 cfg.skills_required 切换两种 hint（集成，绕过 __init__）。"""

    @pytest.mark.skipif(not (SKILLS_DIR / "price-action-trading" / "SKILL.md").exists(),
                        reason="skills 资产缺失")
    def _runner(self, **kw):
        from omnialpha.strategist.loop import PlanRunner

        kw.setdefault("skills", ["price-action-trading"])
        cfg = StrategistConfig(
            bot_id="brooks-eth",
            bot_root=REPO,
            **kw,
        )
        r = PlanRunner.__new__(PlanRunner)  # 只要 self.cfg，绕过重依赖
        r.cfg = cfg
        return r

    def test_catalog_default_mode(self):
        cat = self._runner()._skill_catalog()
        assert "任务匹配某个 skill 时" in cat
        assert "必载" not in cat

    def test_catalog_required_mode(self):
        cat = self._runner(skills_required=True)._skill_catalog()
        assert "技能目录（必载）" in cat
        assert "每轮决策必须先调用 skill(name)" in cat

    def test_catalog_empty_when_no_skills(self):
        r = self._runner(skills=[])
        assert r._skill_catalog() == ""

    def test_catalog_empty_when_required_but_no_skills(self):
        """required=true 但 skills=[]：仍为空——必载开关不越过白名单。"""
        r = self._runner(skills=[], skills_required=True)
        assert r._skill_catalog() == ""
