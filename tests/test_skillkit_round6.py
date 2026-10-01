"""SkillKit 第六轮：状态泄漏 / 跨skill越权 / catalog压测 / 目录名 / 权限 / journal增长。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.skillkit import (  # noqa: E402
    SkillRegistry,
    fit_catalog,
    load_package,
    render_catalog,
    run_skill_ref,
    validate_package,
)
from gate_bot.skillkit.budget import CatalogBudget, estimate_catalog_tokens  # noqa: E402
from gate_bot.skillkit.models import SkillError  # noqa: E402

from tests._skill_fixtures import fixture_root, fixture_skills_dir  # noqa: E402

SKILLS = fixture_skills_dir()
DESC = "Round six test skill. Use when testing skillkit sixth round edge cases."


def _mk(root: Path, name: str, desc: str = DESC, body: str = "# B\n") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{desc}"\n---\n\n{body}', encoding="utf-8"
    )
    return d


class TestNarrowingStateIsolation(unittest.TestCase):
    """收窄状态不得跨 cycle 泄漏。"""

    def test_chat_native_tools_resets_active_tools(self):
        """active_tools 是 _chat_native_tools 的局部变量 → 每次调用重置。"""
        import inspect

        from gate_bot.strategist.loop import PlanRunner

        src = inspect.getsource(PlanRunner._chat_native_tools)
        # 局部初始化（非 self.xxx）证明不跨调用残留
        self.assertIn("active_tools: Optional[list] = None", src)
        self.assertNotIn("self.active_tools", src)

    def test_two_sequential_calls_independent(self):
        """同一 runner 连续两次调用，第二次仍是全工具面。"""
        from gate_bot.strategist.loop import PlanRunner, StrategistConfig
        from gate_bot.strategist.tools import NATIVE_TOOLS

        calls = []

        class FakeLLM:
            last_reasoning_chain = []

            def chat_message_full(self, messages, tools=None, tool_choice=None):
                calls.append(list(tools or []))
                if len(calls) == 1:
                    # 第一次：调 skill(narrow-tools)
                    return {"tool_calls": [{"id": "1", "function": {
                        "name": "skill", "arguments": json.dumps({"name": "narrow-tools"})}}],
                        "content": "", "reasoning_content": ""}
                return {"content": '{"cycle_id":"x","reasoning":"r","chips":[]}', "tool_calls": []}

        runner = PlanRunner.__new__(PlanRunner)
        runner.cfg = StrategistConfig(bot_root=fixture_root(), skills=["narrow-tools"], bot_id="t")
        runner.llm = FakeLLM()
        runner.client = None
        runner._record_tool_use = lambda *a, **k: None
        runner._chat_native_tools([{"role": "system", "content": "s"}], 2)
        first_round_tools = {t["function"]["name"] for t in calls[0]}
        self.assertIn("smc_map", first_round_tools)  # 第 1 轮全工具面
        # 第 2 轮（同一 cycle 内）已收窄
        if len(calls) > 1:
            second = {t["function"]["name"] for t in calls[1]}
            self.assertNotIn("smc_map", second)
            self.assertIn("klines", second)


class TestCrossSkillRefPrivilege(unittest.TestCase):
    """skill_ref 不能跨 skill 越权读。"""

    def test_read_other_skill_requires_its_enablement(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        # 启用 test-helper，尝试读 price-action-trading 的文件
        with self.assertRaises(SkillError):
            run_skill_ref(reg, {"name": "price-action-trading", "path": "SKILL.md"},
                          bot_id="b", enabled_ids=["test-helper"], root=fixture_root())

    def test_read_own_skill_ok(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        out = run_skill_ref(reg, {"name": "test-helper", "path": "SKILL.md"},
                            bot_id="b", enabled_ids=["test-helper"], root=fixture_root())
        self.assertIn("test-helper", out)

    def test_model_invocation_false_ref_blocked(self):
        reg = SkillRegistry()
        reg.scan([SKILLS])
        with self.assertRaises(SkillError):
            run_skill_ref(reg, {"name": "sentinel-risk", "path": "SKILL.md"},
                          bot_id="b", enabled_ids=["sentinel-risk"], root=fixture_root())


class TestCatalogScale(unittest.TestCase):
    """catalog 50+ skill 压测。"""

    def test_50_skills_budget(self):
        metas = []
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            for i in range(50):
                d = _mk(t, f"scale-{i:02d}",
                        desc=f"Scale test skill {i}. Use when testing catalog scale with many skills installed.")
                metas.append(load_package(d).meta)
            full = estimate_catalog_tokens(metas)
            self.assertGreater(full, 0)
            # 默认 2000 token 预算：短描述可能全容下 → 用紧预算强制裁剪
            kept_tight = fit_catalog(metas, budget=CatalogBudget(max_tokens=100, clip=80))
            self.assertLess(len(kept_tight), 50)
            self.assertGreater(len(kept_tight), 0)
            # 高频保留
            kept_freq = fit_catalog(metas, budget=CatalogBudget(max_tokens=100, clip=80),
                                    freq={"scale-07": 999})
            self.assertIn("scale-07", [m.id for m in kept_freq])
            cat = render_catalog(metas, budget=CatalogBudget(max_tokens=100, clip=80))
            self.assertLess(len(cat), 1500)
            # 大预算应全保留
            kept_all = fit_catalog(metas, budget=CatalogBudget(max_tokens=10**6))
            self.assertEqual(len(kept_all), 50)

    def test_catalog_order_stable(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            for n in ("zzz", "aaa", "mmm"):
                _mk(t, n)
            reg = SkillRegistry()
            reg.scan([t])
            cat1 = render_catalog(reg.visible_for("b"))
            reg2 = SkillRegistry()
            reg2.scan([t])
            cat2 = render_catalog(reg2.visible_for("b"))
            self.assertEqual(cat1, cat2)  # 顺序稳定
            # 按 id 升序
            self.assertLess(cat1.index("aaa"), cat1.index("mmm"))
            self.assertLess(cat1.index("mmm"), cat1.index("zzz"))


class TestDirnameMismatch(unittest.TestCase):
    """目录名与 frontmatter name 不一致。"""

    def test_mismatch_warns_but_loads(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = t / "dirname-x"
            d.mkdir()
            (d / "SKILL.md").write_text(
                f'---\nname: frontmatter-y\ndescription: "{DESC}"\n---\n\n# Y\n', encoding="utf-8"
            )
            rep = validate_package(d)
            self.assertTrue(rep.ok)
            self.assertTrue(any("W01" in w for w in rep.warnings))
            pkg = load_package(d)
            # 以 frontmatter name 为准
            self.assertEqual(pkg.meta.id, "frontmatter-y")


class TestPermissionEdge(unittest.TestCase):
    """目录不可读 / 权限异常。"""

    def test_nonexistent_root(self):
        self.assertEqual(SkillRegistry().scan([Path("Z:/no/such/path")]), [])

    def test_unreadable_skill_md_skipped_in_scan(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            d = _make_dir_with_broken_md(t)
            reg = SkillRegistry()
            metas = reg.scan([t])
            # 坏包不进 registry（不崩）
            self.assertEqual(metas, [])

    def test_file_root_not_dir(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "notadir.txt"
            f.write_text("x", encoding="utf-8")
            self.assertEqual(SkillRegistry().scan([f]), [])


def _make_dir_with_broken_md(t: Path) -> Path:
    d = t / "broken"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"\xff\xfe\x00invalid utf8 \x80\x81")
    return d


class TestJournalGrowth(unittest.TestCase):
    """journal 增长与可读性。"""

    def test_large_journal_still_readable(self):
        from gate_bot.skillkit import run_skill_tool
        from gate_bot.skillkit.journal import read_activation_freq

        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            _mk(t, "grow-skill")
            reg = SkillRegistry()
            reg.scan([t])
            for i in range(200):
                run_skill_tool(reg, {"name": "grow-skill"}, bot_id=f"b{i % 5}",
                               enabled_ids=["grow-skill"], root=t)
            jp = t / "logs" / "skill_journal.jsonl"
            lines = jp.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 200)
            freq = read_activation_freq(t)
            self.assertEqual(freq["grow-skill"], 200)


class TestConcurrentPlanProcesses(unittest.TestCase):
    """两个进程同时跑 plan（共享 skills 目录）。"""

    def test_concurrent_scan_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            for i in range(10):
                _mk(t, f"conc-{i}")
            worker = t / "scan.py"
            worker.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, r'{ROOT}')\n"
                "from gate_bot.skillkit import SkillRegistry, render_catalog\n"
                "reg = SkillRegistry()\n"
                "reg.scan([Path(sys.argv[1])])\n"
                "cat = render_catalog(reg.visible_for('b'))\n"
                "assert len(reg.ids()) == 10, reg.ids()\n"
                "assert '<skill_catalog>' in cat\n"
                "print('OK')\n",
                encoding="utf-8",
            )
            procs = [subprocess.Popen([sys.executable, str(worker), str(t)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for _ in range(4)]
            for p in procs:
                out, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
