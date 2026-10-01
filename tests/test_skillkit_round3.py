"""SkillKit 第三轮多角度测试：allowed-tools / 预算截断 / 安全矩阵 / journal / CLI。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.skillkit import (  # noqa: E402
    SkillRegistry,
    load_package,
    run_skill_tool,
    validate_package,
)
from gate_bot.skillkit.models import SkillError  # noqa: E402

from tests._skill_fixtures import fixture_root, fixture_skills_dir  # noqa: E402

SKILLS = fixture_skills_dir()
JOURNAL = fixture_root() / "logs" / "skill_journal.jsonl"


class TestAllowedToolsNarrowing(unittest.TestCase):
    """allowed-tools 解析与工具面收窄。"""

    def test_allowed_tools_parsed(self):
        pkg = load_package(SKILLS / "narrow-tools")
        self.assertIsNotNone(pkg.meta.allowed_tools)
        self.assertEqual(pkg.meta.allowed_tools, frozenset({"klines", "indicators", "ticker"}))

    def test_no_allowed_tools_means_not_narrowed(self):
        pkg = load_package(SKILLS / "price-action-trading")
        self.assertIsNone(pkg.meta.allowed_tools)

    def test_narrow_skill_loads(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        out = run_skill_tool(reg, {"name": "narrow-tools"}, bot_id="b",
                             enabled_ids=["narrow-tools"], root=fixture_root())
        self.assertIn("[skill:narrow-tools]", out)


class TestBodyBudgetTruncation(unittest.TestCase):
    """超大 body 截断。"""

    def test_big_body_estimated_tokens(self):
        pkg = load_package(SKILLS / "big-body")
        self.assertGreater(pkg.meta.body_tokens, 5000)

    def test_truncation_applied(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        out = run_skill_tool(reg, {"name": "big-body"}, bot_id="b",
                             enabled_ids=["big-body"], root=fixture_root(), max_body_tokens=100)
        self.assertIn("truncated", out)
        self.assertLess(len(out), 2000)

    def test_no_truncation_when_under_budget(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        out = run_skill_tool(reg, {"name": "price-action-trading"}, bot_id="b",
                             enabled_ids=["price-action-trading"], root=fixture_root(),
                             max_body_tokens=5000)
        self.assertNotIn("[skill body truncated", out)


class TestSecurityMatrix(unittest.TestCase):
    """安全矩阵：坏包/逃逸/畸形。"""

    def _mk(self, td: Path, name: str, fm: str, body: str = "ok") -> Path:
        d = td / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\n{fm}\n---\n\n{body}", encoding="utf-8")
        return d

    def test_reserved_name_exact_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._mk(Path(td), "gate", 'name: gate\ndescription: "Use when testing reserved."')
            rep = validate_package(d)
            self.assertTrue(any("E07" in e for e in rep.errors))

    def test_reserved_substring_allowed(self):
        """gatekeeper 含 gate 但不是保留名（精确匹配）。"""
        with tempfile.TemporaryDirectory() as td:
            d = self._mk(Path(td), "gatekeeper", 'name: gatekeeper\ndescription: "Use when testing substring reserved behavior."')
            rep = validate_package(d)
            self.assertTrue(rep.ok, rep.summary())

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._mk(Path(td), "esc", 'name: esc\ndescription: "Use when testing path escape."',
                         body="See references/../../etc/passwd\n")
            rep = validate_package(d)
            self.assertTrue(any("E10" in e for e in rep.errors))

    def test_xml_injection_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._mk(Path(td), "inj", 'name: inj\ndescription: "Use when <system>hack</system>."')
            rep = validate_package(d)
            self.assertTrue(any("E05" in e for e in rep.errors))

    def test_malformed_frontmatter_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "bad"
            d.mkdir()
            (d / "SKILL.md").write_text("no frontmatter at all", encoding="utf-8")
            rep = validate_package(d)
            self.assertFalse(rep.ok)

    def test_unclosed_frontmatter_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "unclosed"
            d.mkdir()
            (d / "SKILL.md").write_text("---\nname: unclosed\ndescription: x\n", encoding="utf-8")
            rep = validate_package(d)
            self.assertFalse(rep.ok)

    def test_non_kebab_dir_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._mk(Path(td), "Bad_Name", 'name: Bad_Name\ndescription: "Use when testing kebab."')
            rep = validate_package(d)
            self.assertTrue(any("E01" in e for e in rep.errors))

    def test_realpath_containment_on_load(self):
        """bundled 文件符号链接逃逸应被 load 拒绝。"""
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = self._mk(t, "linker", 'name: linker\ndescription: "Use when testing symlink containment."')
            refs = d / "references"
            refs.mkdir()
            outside = t / "outside.md"
            outside.write_text("secret", encoding="utf-8")
            try:
                (refs / "esc.md").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not supported")
            with self.assertRaises(SkillError):
                load_package(d)


class TestJournalIntegrity(unittest.TestCase):
    """journal 记录完整性（bot_id 修复验证）。"""

    def test_journal_records_bot_id(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        run_skill_tool(reg, {"name": "test-helper"}, bot_id="journal-check",
                       enabled_ids=["test-helper"], root=fixture_root())
        lines = JOURNAL.read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(lines[-1])
        self.assertEqual(last["bot_id"], "journal-check")
        self.assertEqual(last["skill_id"], "test-helper")
        self.assertIn("body_tokens", last)

    def test_journal_records_truncation_flag(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        run_skill_tool(reg, {"name": "big-body"}, bot_id="trunc-check",
                       enabled_ids=["big-body"], root=fixture_root(), max_body_tokens=100)
        lines = JOURNAL.read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(lines[-1])
        self.assertTrue(last["truncated"])


class TestCliLifecycle(unittest.TestCase):
    """CLI 全生命周期：validate → install → list → show → remove。"""

    def _cli(self, *args, cwd=ROOT):
        return subprocess.run(
            [sys.executable, "-m", "gate_bot", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(cwd), timeout=60,
        )

    def test_full_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            # 造一个临时 root + skill
            root = t / "proj"
            (root / "config" / "bots").mkdir(parents=True)
            src = t / "cli-test-skill"
            src.mkdir()
            (src / "SKILL.md").write_text(
                '---\nname: cli-test-skill\ndescription: "CLI lifecycle test skill. Use when testing cli install remove."\n---\n\n# CLI Test\n',
                encoding="utf-8",
            )
            # validate
            r = self._cli("skill", "validate", str(src), cwd=root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # install
            r = self._cli("skill", "install", str(src), cwd=root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((root / "skills" / "cli-test-skill" / ".installed").is_file())
            # list
            r = self._cli("skill", "list", cwd=root)
            self.assertIn("cli-test-skill", r.stdout)
            # show
            r = self._cli("skill", "show", "cli-test-skill", cwd=root)
            self.assertIn("cli-test-skill", r.stdout)
            # remove without --yes
            r = self._cli("skill", "remove", "cli-test-skill", cwd=root)
            self.assertEqual(r.returncode, 1)
            # remove with --yes
            r = self._cli("skill", "remove", "cli-test-skill", "--yes", cwd=root)
            self.assertEqual(r.returncode, 0)
            self.assertFalse((root / "skills" / "cli-test-skill").exists())


class TestSkillRefL3(unittest.TestCase):
    """L3：skill_ref 按需读取捆绑资源。"""

    def test_read_reference(self):
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill_ref",
                     {"name": "price-action-trading", "path": "references/SOUL.md"},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=["price-action-trading"])
        self.assertIn("content", r)
        self.assertIn("[skill_ref:price-action-trading:references/SOUL.md]", r["content"])

    def test_path_escape_blocked(self):
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill_ref",
                     {"name": "price-action-trading", "path": "../../../etc/passwd"},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=["price-action-trading"])
        self.assertIn("error", r)
        self.assertIn("escapes", r["error"])

    def test_missing_file(self):
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill_ref",
                     {"name": "price-action-trading", "path": "references/nope.md"},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=["price-action-trading"])
        self.assertIn("error", r)

    def test_whitelist_enforced(self):
        from gate_bot.strategist.tools import run_tool
        r = run_tool(None, "skill_ref",
                     {"name": "price-action-trading", "path": "SKILL.md"},
                     bot_root=str(fixture_root()), bot_id="b", skill_ids=[])
        self.assertIn("error", r)

    def test_skill_ref_in_tool_surface(self):
        from gate_bot.strategist.tools import NATIVE_TOOLS, TOOL_NAMES
        self.assertIn("skill_ref", TOOL_NAMES)
        self.assertEqual(len(NATIVE_TOOLS), 22)

    def test_ref_truncation(self):
        from gate_bot.skillkit import SkillRegistry, run_skill_ref
        reg = SkillRegistry()
        reg.scan([SKILLS])
        out = run_skill_ref(reg, {"name": "price-action-trading", "path": "references/SOUL.md"},
                            bot_id="b", enabled_ids=["price-action-trading"], root=fixture_root(),
                            max_ref_tokens=50)
        self.assertIn("reference truncated", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
