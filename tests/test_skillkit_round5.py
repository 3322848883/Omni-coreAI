"""SkillKit 第五轮：同名冲突 / 嵌套发现 / frontmatter 边界 / 编码 / 并发 / 二进制。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.skillkit import (  # noqa: E402
    SkillRegistry,
    load_package,
    parse_frontmatter,
    render_catalog,
    run_skill_ref,
    run_skill_tool,
    scan_skill_dirs,
    validate_package,
)
from omnialpha.skillkit.models import SkillError  # noqa: E402

from tests._skill_fixtures import fixture_root, fixture_skills_dir  # noqa: E402

SKILLS = fixture_skills_dir()


def _mk(root: Path, name: str, desc: str, body: str = "# B\n", extra: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{desc}"\n{extra}---\n\n{body}', encoding="utf-8"
    )
    return d


DESC = "Round five test skill. Use when testing skillkit edge cases."


class TestSameNameConflict(unittest.TestCase):
    """同名 skill 跨根优先级。"""

    def test_higher_priority_root_wins(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            hi = t / "hi"
            lo = t / "lo"
            _mk(hi, "dup", DESC, body="# FROM-HI\n")
            _mk(lo, "dup", DESC, body="# FROM-LO\n")
            reg = SkillRegistry()
            reg.scan([hi, lo])
            pkg = reg.get_package("dup")
            self.assertIn("FROM-HI", pkg.body)

    def test_reverse_order_changes_winner(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            a = t / "a"
            b = t / "b"
            _mk(a, "dup", DESC, body="# FROM-A\n")
            _mk(b, "dup", DESC, body="# FROM-B\n")
            reg = SkillRegistry()
            reg.scan([b, a])  # b 优先
            self.assertIn("FROM-B", reg.get_package("dup").body)

    def test_no_silent_duplicate_in_catalog(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            _mk(t / "x", "dup", DESC)
            _mk(t / "y", "dup", DESC)
            reg = SkillRegistry()
            reg.scan([t / "x", t / "y"])
            ids = reg.ids()
            self.assertEqual(ids.count("dup"), 1)


class TestNestedDiscovery(unittest.TestCase):
    """嵌套目录发现规则。"""

    def test_namespaced_skill_found(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = t / "ns" / "inner-skill"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f'---\nname: inner-skill\ndescription: "{DESC}"\n---\n\n# I\n', encoding="utf-8")
            found = scan_skill_dirs(t)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].name, "inner-skill")

    def test_stops_at_first_skill_md(self):
        """父目录有 SKILL.md 则不再向下（避免父包吞子包）。"""
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            parent = _mk(t, "parent", DESC)
            _mk(parent, "child", DESC)  # parent/child/SKILL.md
            found = scan_skill_dirs(t)
            names = sorted(p.name for p in found)
            self.assertEqual(names, ["parent"])  # 不递归进 parent

    def test_dir_without_skill_md_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            (t / "notaskill").mkdir()
            (t / "notaskill" / "readme.txt").write_text("x", encoding="utf-8")
            self.assertEqual(scan_skill_dirs(t), [])

    def test_depth_limit(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            deep = t
            for i in range(9):
                deep = deep / f"d{i}"
            deep.mkdir(parents=True)
            (deep / "SKILL.md").write_text(f'---\nname: deep-skill\ndescription: "{DESC}"\n---\n\n# D\n', encoding="utf-8")
            # 深度超 6 → 不发现
            self.assertEqual(scan_skill_dirs(t), [])


class TestFrontmatterEdgeCases(unittest.TestCase):
    """frontmatter 解析边界。"""

    def test_colon_in_value(self):
        fields, _ = parse_frontmatter('---\nname: a-b\ndescription: "Use when: testing colons"\n---\nbody')
        self.assertEqual(fields["description"], "Use when: testing colons")

    def test_single_quotes(self):
        fields, _ = parse_frontmatter("---\nname: a-b\ndescription: 'Use when x'\n---\nbody")
        self.assertEqual(fields["description"], "Use when x")

    def test_unquoted_value(self):
        fields, _ = parse_frontmatter("---\nname: a-b\ndescription: Use when plain\n---\nbody")
        self.assertEqual(fields["description"], "Use when plain")

    def test_multiline_folded(self):
        fields, _ = parse_frontmatter('---\nname: a-b\ndescription: "Use when\n  folded line"\n---\nbody')
        self.assertIn("folded line", fields["description"])

    def test_comment_lines_ignored(self):
        fields, _ = parse_frontmatter("---\n# comment\nname: a-b\ndescription: x\n---\nbody")
        self.assertEqual(fields["name"], "a-b")

    def test_crlf_line_endings(self):
        fields, body = parse_frontmatter("---\r\nname: a-b\r\ndescription: x\r\n---\r\nbody")
        self.assertEqual(fields["name"], "a-b")

    def test_unicode_description(self):
        d = "价格行为交易辅助：H2/L2 入场、交易者方程、BAN 禁止清单。Use when 用户要求分析 K 线。"
        fields, _ = parse_frontmatter(f'---\nname: a-b\ndescription: "{d}"\n---\nbody')
        self.assertEqual(fields["description"], d)

    def test_emoji_in_description(self):
        d = "Test skill 🎯 with emoji. Use when testing emoji handling."
        fields, _ = parse_frontmatter(f'---\nname: a-b\ndescription: "{d}"\n---\nbody')
        self.assertEqual(fields["description"], d)

    def test_empty_frontmatter_block(self):
        fields, body = parse_frontmatter("---\n---\nbody")
        self.assertEqual(fields, {})

    def test_value_with_hash(self):
        fields, _ = parse_frontmatter('---\nname: a-b\ndescription: "Use when # not a comment"\n---\nbody')
        self.assertIn("# not a comment", fields["description"])


class TestUnicodeSkillEndToEnd(unittest.TestCase):
    """中文/emoji skill 全链路。"""

    def test_chinese_skill_loads_and_validates(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = t / "chinese-skill"
            d.mkdir()
            (d / "SKILL.md").write_text(
                '---\nname: chinese-skill\ndescription: "中文技能：用于测试中文描述与正文。Use when 用户要求中文测试。"\n---\n\n# 中文技能\n\n正文含中文与 emoji 🚀\n',
                encoding="utf-8",
            )
            rep = validate_package(d)
            self.assertTrue(rep.ok, rep.summary())
            pkg = load_package(d)
            self.assertIn("中文技能", pkg.body)
            self.assertGreater(pkg.meta.body_tokens, 0)

    def test_chinese_catalog_render(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = t / "cn-skill"
            d.mkdir()
            (d / "SKILL.md").write_text(
                '---\nname: cn-skill\ndescription: "中文目录渲染测试。Use when 测试中文。"\n---\n\n正文\n',
                encoding="utf-8",
            )
            reg = SkillRegistry()
            reg.scan([t])
            cat = render_catalog(reg.visible_for("b"))
            self.assertIn("cn-skill", cat)
            self.assertIn("中文目录渲染测试", cat)


class TestSkillRefBinaryAndLarge(unittest.TestCase):
    """skill_ref 读二进制/超大文件。"""

    def test_binary_file_read_safe(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = _mk(t, "bin-skill", DESC)
            assets = d / "assets"
            assets.mkdir()
            (assets / "blob.bin").write_bytes(bytes(range(256)) * 4)
            reg = SkillRegistry()
            reg.scan([t])
            out = run_skill_ref(reg, {"name": "bin-skill", "path": "assets/blob.bin"},
                                bot_id="b", enabled_ids=["bin-skill"], root=t)
            self.assertIn("[skill_ref:bin-skill:assets/blob.bin]", out)

    def test_large_file_truncated(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = _mk(t, "big-ref", DESC)
            refs = d / "references"
            refs.mkdir()
            (refs / "huge.md").write_text("word " * 50000, encoding="utf-8")
            reg = SkillRegistry()
            reg.scan([t])
            out = run_skill_ref(reg, {"name": "big-ref", "path": "references/huge.md"},
                                bot_id="b", enabled_ids=["big-ref"], root=t, max_ref_tokens=200)
            self.assertIn("reference truncated", out)
            self.assertLess(len(out), 3000)

    def test_directory_as_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = _mk(t, "dir-skill", DESC)
            (d / "references").mkdir()
            reg = SkillRegistry()
            reg.scan([t])
            with self.assertRaises(SkillError):
                run_skill_ref(reg, {"name": "dir-skill", "path": "references"},
                              bot_id="b", enabled_ids=["dir-skill"], root=t)


class TestConcurrentJournalWrites(unittest.TestCase):
    """并发写 journal 不损坏。"""

    def test_threaded_writes_all_parsable(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            _mk(t, "conc-skill", DESC)
            reg = SkillRegistry()
            reg.scan([t])
            errors = []

            def worker(i):
                try:
                    run_skill_tool(reg, {"name": "conc-skill"}, bot_id=f"th-{i}",
                                   enabled_ids=["conc-skill"], root=t)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            jp = t / "logs" / "skill_journal.jsonl"
            lines = jp.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 20)
            for ln in lines:
                json.loads(ln)  # 每行完整


class TestCrossProcessJournal(unittest.TestCase):
    """跨进程并发写 journal 不丢写（OS 文件锁）。"""

    def test_multiprocess_no_lost_writes(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            worker = t / "w.py"
            worker.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, r'{ROOT}')\n"
                "from omnialpha.skillkit.journal import append_journal\n"
                "root = Path(sys.argv[1]); n = int(sys.argv[2])\n"
                "for i in range(n):\n"
                "    append_journal(root, {'kind': 'proc', 'skill_id': 'x', 'i': i})\n",
                encoding="utf-8",
            )
            n_proc, n_each = 4, 30
            procs = [subprocess.Popen([sys.executable, str(worker), str(t), str(n_each)])
                     for _ in range(n_proc)]
            for p in procs:
                p.wait()
            lines = (t / "logs" / "skill_journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), n_proc * n_each)
            for ln in lines:
                json.loads(ln)


if __name__ == "__main__":
    unittest.main(verbosity=2)
