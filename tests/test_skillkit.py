"""SkillKit 引擎测试：loader / validate / registry / budget / catalog / tool。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.skillkit import (  # noqa: E402
    SkillError,
    SkillRegistry,
    fit_catalog,
    load_package,
    parse_frontmatter,
    render_catalog,
    run_skill_tool,
    validate_package,
)
from gate_bot.skillkit.models import SkillMeta  # noqa: E402


def _write_skill(root: Path, name: str, desc: str, body: str = "# Body\n\nDo the thing.\n",
                 extra_front: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: \"{desc}\"\n{extra_front}---\n\n{body}",
        encoding="utf-8",
    )
    return d


GOOD_DESC = "Test skill for unit tests. Use when the user asks to test the skillkit engine."


class TestLoader(unittest.TestCase):
    def test_parse_frontmatter_basic(self):
        fields, body = parse_frontmatter("---\nname: abc\ndescription: hi\n---\n\nBody here")
        self.assertEqual(fields["name"], "abc")
        self.assertEqual(body.strip(), "Body here")

    def test_parse_frontmatter_none(self):
        fields, body = parse_frontmatter("no frontmatter")
        self.assertIsNone(fields)
        self.assertEqual(body, "no frontmatter")

    def test_angle_brackets_rejected(self):
        with self.assertRaises(SkillError):
            parse_frontmatter("---\nname: abc\nbad: <script>\n---\nbody")

    def test_unclosed_frontmatter(self):
        with self.assertRaises(SkillError):
            parse_frontmatter("---\nname: abc\nbody without close")

    def test_load_package_ok(self):
        with tempfile.TemporaryDirectory() as td:
            d = _write_skill(Path(td), "my-skill", GOOD_DESC)
            (d / "references").mkdir()
            (d / "references" / "more.md").write_text("extra", encoding="utf-8")
            pkg = load_package(d)
            self.assertEqual(pkg.meta.id, "my-skill")
            self.assertIn("references/more.md", pkg.files)
            self.assertGreater(pkg.meta.body_tokens, 0)

    def test_name_must_be_kebab(self):
        with tempfile.TemporaryDirectory() as td:
            d = _write_skill(Path(td), "my-skill", GOOD_DESC)
            # 改成非法 name
            (d / "SKILL.md").write_text(
                f"---\nname: My_Skill\ndescription: \"{GOOD_DESC}\"\n---\n\nBody",
                encoding="utf-8",
            )
            with self.assertRaises(SkillError):
                load_package(d)

    def test_reserved_name_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "gate"
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\nname: gate\ndescription: \"{GOOD_DESC}\"\n---\n\nBody", encoding="utf-8"
            )
            with self.assertRaises(SkillError):
                load_package(d)


class TestValidate(unittest.TestCase):
    def test_good_package_pass(self):
        with tempfile.TemporaryDirectory() as td:
            d = _write_skill(Path(td), "ok-skill", GOOD_DESC)
            rep = validate_package(d)
            self.assertTrue(rep.ok, rep.summary())

    def test_e02_missing_skill_md(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "empty-skill"
            d.mkdir()
            rep = validate_package(d)
            self.assertFalse(rep.ok)
            self.assertTrue(any("E02" in e for e in rep.errors))

    def test_e03_readme_forbidden(self):
        with tempfile.TemporaryDirectory() as td:
            d = _write_skill(Path(td), "rm-skill", GOOD_DESC)
            (d / "README.md").write_text("x", encoding="utf-8")
            rep = validate_package(d)
            self.assertTrue(any("E03" in e for e in rep.errors))

    def test_e05_angle_brackets(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "bad-skill"
            d.mkdir()
            (d / "SKILL.md").write_text(
                '---\nname: bad-skill\ndescription: "Use when <b>test</b>."\n---\n\nBody',
                encoding="utf-8",
            )
            rep = validate_package(d)
            self.assertTrue(any("E05" in e for e in rep.errors))

    def test_e08_description_too_long(self):
        with tempfile.TemporaryDirectory() as td:
            long_desc = "Use when testing. " + "x" * 1100
            d = Path(td) / "long-skill"
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\nname: long-skill\ndescription: \"{long_desc}\"\n---\n\nBody",
                encoding="utf-8",
            )
            rep = validate_package(d)
            self.assertTrue(any("E08" in e for e in rep.errors))

    def test_w03_no_when_clause(self):
        with tempfile.TemporaryDirectory() as td:
            d = _write_skill(Path(td), "vague-skill", "This skill does stuff for projects.")
            rep = validate_package(d)
            self.assertTrue(any("W03" in w for w in rep.warnings))
            self.assertTrue(rep.ok)  # warning 不阻断

    def test_w05_missing_reference(self):
        with tempfile.TemporaryDirectory() as td:
            d = _write_skill(
                Path(td), "ref-skill", GOOD_DESC,
                body="Read references/missing.md first.\n",
            )
            rep = validate_package(d)
            self.assertTrue(any("W05" in w for w in rep.warnings))

    def test_e10_path_escape(self):
        with tempfile.TemporaryDirectory() as td:
            d = _write_skill(
                Path(td), "esc-skill", GOOD_DESC,
                body="See references/../../../etc/passwd\n",
            )
            rep = validate_package(d)
            self.assertTrue(any("E10" in e for e in rep.errors))


class TestRegistry(unittest.TestCase):
    def _make(self, td: Path, name: str) -> Path:
        return _write_skill(td, name, f"{name} skill. Use when testing {name}.")

    def test_scan_and_priority(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            proj = t / "proj"
            glob = t / "glob"
            proj.mkdir()
            glob.mkdir()
            self._make(proj, "dup-skill")
            self._make(glob, "dup-skill")
            self._make(glob, "only-glob")
            reg = SkillRegistry()
            metas = reg.scan([proj, glob])
            ids = {m.id for m in metas}
            self.assertIn("dup-skill", ids)
            self.assertIn("only-glob", ids)
            self.assertEqual(len(ids), 2)  # 同名去重
            # 优先 proj（先扫到）
            self.assertEqual(reg.get_meta("dup-skill").path, (proj / "dup-skill").resolve())

    def test_visibility(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            self._make(t, "alpha-skill")
            self._make(t, "beta-skill")
            reg = SkillRegistry()
            reg.scan([t])
            self.assertEqual(len(reg.visible_for("b")), 2)      # 默认全可见
            self.assertEqual(len(reg.visible_for("b", [])), 0)   # 空 = 无
            vis = reg.visible_for("b", ["alpha-skill"])
            self.assertEqual([m.id for m in vis], ["alpha-skill"])


class TestBudgetCatalog(unittest.TestCase):
    def test_fit_catalog_overflow(self):
        metas = [
            SkillMeta(id=f"skill-{i:02d}", description="Use when testing. " + "d" * 200, path=Path("."))
            for i in range(50)
        ]
        kept = fit_catalog(metas, budget=None, freq={"skill-05": 100})
        self.assertLess(len(kept), 50)
        self.assertIn("skill-05", [m.id for m in kept])  # 高频保留

    def test_render_catalog(self):
        metas = [SkillMeta(id="abc", description="Use when testing.", path=Path("."))]
        cat = render_catalog(metas)
        self.assertIn("<skill_catalog>", cat)
        self.assertIn("- abc:", cat)
        self.assertEqual(render_catalog([]), "")


class TestSkillTool(unittest.TestCase):
    def test_run_skill_tool_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = _write_skill(t, "load-me", GOOD_DESC, body="# Instruction\n\nDo X.")
            reg = SkillRegistry()
            reg.scan([t])
            out = run_skill_tool(reg, {"name": "load-me"}, bot_id="b", enabled_ids=["load-me"], root=t)
            self.assertTrue(out.startswith("[skill:load-me]"))
            self.assertIn("Do X.", out)
            journal = t / "logs" / "skill_journal.jsonl"
            self.assertTrue(journal.is_file())

    def test_unknown_skill(self):
        reg = SkillRegistry()
        with self.assertRaises(SkillError):
            run_skill_tool(reg, {"name": "nope"})

    def test_not_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            _write_skill(t, "locked", GOOD_DESC)
            reg = SkillRegistry()
            reg.scan([t])
            with self.assertRaises(SkillError):
                run_skill_tool(reg, {"name": "locked"}, bot_id="b", enabled_ids=["other"])

    def test_model_invocation_false_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = _write_skill(
                t, "user-only", GOOD_DESC,
                extra_front="model-invocation: false\n",
            )
            pkg = load_package(d)
            self.assertFalse(pkg.meta.model_invocation)
            reg = SkillRegistry()
            reg.scan([t])
            with self.assertRaises(SkillError):
                run_skill_tool(reg, {"name": "user-only"})


class TestToolsIntegration(unittest.TestCase):
    def test_native_tools_has_skill(self):
        from gate_bot.strategist.tools import NATIVE_TOOLS, TOOL_NAMES
        self.assertIn("skill", TOOL_NAMES)
        self.assertIn("skill_ref", TOOL_NAMES)
        self.assertEqual(len(NATIVE_TOOLS), 25)  # 22 基础 + 3 TV

    def test_prompt_catalog_slot(self):
        from gate_bot.strategist.prompt import build_system_prompt
        sys_prompt = build_system_prompt("persona", skill_catalog="<skill_catalog>\n- x: y\n</skill_catalog>")
        self.assertIn("<skill_catalog>", sys_prompt)
        self.assertIn("persona", sys_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
