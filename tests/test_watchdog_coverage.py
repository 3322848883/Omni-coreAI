"""watchdog 守护覆盖：组级（persona-run）与全局（broadcast）进程。

回归背景（2026-10-05 架构盘点 S17）：watchdog 的「应有组件」清单原先只覆盖
plan / run / paper 三类 —— `persona-run`、`broadcast` 都不在清单里。
`persona-run` 是讨论组的大脑，它挂了意味着「保护单有人管、但没人再决策」，
而没有任何东西会发现并拉起它。

**两条 opt-in 规则（都不是「存在即守护」）**：
  - persona：组要 `enabled: true` **且** `runtime.persona_run: true`。
    只看 `enabled` 不行 —— 本机 persona_groups.yaml 有 9 个组都是 true
    （含 4 个对照实验组），按 `enabled` 守护会让本地一启动 watchdog 就拉起
    9 个 persona-run，每个都在跑真实 LLM 分析。
  - broadcast：`config/broadcast.yaml` 里至少有一条 `enabled: true` 的路由。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.watchdog import Target, Watchdog  # noqa: E402

_ENTRY = (
    "  - name: {name}\n    topology: single_account\n"
    "    members: [b1, b2]\n    target_account: b1\n"
    "    fusion: weighted_vote\n    enabled: {enabled}\n{runtime}"
)


def _mk_root(td: str, *, with_persona=True, with_broadcast=True,
             runtime="    runtime: {persona_run: true}\n") -> Path:
    root = Path(td)
    cfg = root / "config"
    (cfg / "bots").mkdir(parents=True, exist_ok=True)
    (cfg / "bots" / "b1.yaml").write_text(
        "bot_id: b1\nenv: live\nsymbols: [BTC_USDT]\n", encoding="utf-8")
    # `enabled` 只认 overlay（基线里写 true 会被忽略并告警）—— 所以启用要写这里
    (cfg / "bots.local").mkdir(parents=True, exist_ok=True)
    (cfg / "bots.local" / "b1.yaml").write_text(
        "enabled: true\n", encoding="utf-8")
    if with_persona:
        (cfg / "persona_groups.yaml").write_text(
            "groups:\n"
            + _ENTRY.format(name="g1", enabled="true", runtime=runtime)
            + _ENTRY.format(name="g-off", enabled="false", runtime=runtime)
            + _ENTRY.format(name="g-noguard", enabled="true", runtime=""),
            encoding="utf-8")
    if with_broadcast:
        (cfg / "broadcast.yaml").write_text("routes: []\n", encoding="utf-8")
    return root


class TestDiscoverGlobals(unittest.TestCase):
    def _components(self, wd):
        return {(t.bot_id, t.component) for t in wd.discover()}

    def test_persona_guarded_when_opted_in(self):
        with tempfile.TemporaryDirectory() as td:
            comps = self._components(Watchdog(_mk_root(td)))
            self.assertIn(("persona:g1", "persona"), comps)

    def test_disabled_group_not_guarded(self):
        with tempfile.TemporaryDirectory() as td:
            comps = self._components(Watchdog(_mk_root(td)))
            self.assertNotIn(("persona:g-off", "persona"), comps)

    def test_enabled_but_not_opted_in_is_not_guarded(self):
        """`enabled: true` 但没写 `runtime.persona_run` → 不守护。

        这是本机不炸的关键：9 个组都是 enabled，只有显式开的那一个该被拉起。
        """
        with tempfile.TemporaryDirectory() as td:
            comps = self._components(Watchdog(_mk_root(td)))
            self.assertNotIn(("persona:g-noguard", "persona"), comps)

    def test_explicit_false_is_not_guarded(self):
        with tempfile.TemporaryDirectory() as td:
            comps = self._components(
                Watchdog(_mk_root(td, runtime="    runtime: {persona_run: false}\n")))
            self.assertNotIn(("persona:g1", "persona"), comps)

    def test_broadcast_needs_an_enabled_route(self):
        """路由全关 → 不守护（否则只是白起一个空转进程）。"""
        with tempfile.TemporaryDirectory() as td:
            comps = self._components(Watchdog(_mk_root(td)))
            self.assertNotIn(("broadcast", "broadcast"), comps)

    def test_broadcast_guarded_when_a_route_is_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            root = _mk_root(td)
            (root / "config" / "broadcast.yaml").write_text(
                "routes:\n  - name: r1\n    from: a\n    to: [b]\n    enabled: true\n",
                encoding="utf-8")
            comps = self._components(Watchdog(root))
            self.assertIn(("broadcast", "broadcast"), comps)

    def test_skipped_when_bot_ids_given(self):
        """显式指定 bot 列表时不该顺带拉起全局进程。"""
        with tempfile.TemporaryDirectory() as td:
            wd = Watchdog(_mk_root(td), bot_ids=["b1"])
            comps = {t.component for t in wd.discover()}
            self.assertNotIn("persona", comps)
            self.assertNotIn("broadcast", comps)

    def test_missing_configs_are_safe(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Watchdog(_mk_root(td, with_persona=False, with_broadcast=False))
            comps = {t.component for t in wd.discover()}
            self.assertNotIn("persona", comps)
            self.assertNotIn("broadcast", comps)

    def test_broken_yaml_does_not_crash_discovery(self):
        """配置写坏时不该把整个 watchdog 带崩 —— 其余 bot 仍要能被守护。"""
        with tempfile.TemporaryDirectory() as td:
            root = _mk_root(td)
            (root / "config" / "persona_groups.yaml").write_text(
                "groups: [oops\n", encoding="utf-8")
            comps = {t.component for t in Watchdog(root).discover()}
            self.assertIn("run", comps)


class TestShippedConfig(unittest.TestCase):
    """本机真实配置下，只该守护 eth-disc 这一个组。"""

    def test_only_ethdisc_is_guarded(self):
        wd = Watchdog(ROOT)
        personas = sorted(t.bot_id for t in wd.discover() if t.component == "persona")
        self.assertEqual(personas, ["persona:eth-disc"])

    def test_broadcast_not_guarded_today(self):
        wd = Watchdog(ROOT)
        self.assertNotIn("broadcast", {t.component for t in wd.discover()})


class TestAliveProbe(unittest.TestCase):
    def test_persona_lock_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "data" / "shared"
            d.mkdir(parents=True, exist_ok=True)
            # 锁文件不存在 → 不活着
            self.assertFalse(Watchdog._alive("persona:g1", "persona", root))
            (d / "persona-g1.lock").write_text("", encoding="utf-8")
            # 存在但没被持有 → 也不活着（探测能拿到锁）
            self.assertFalse(Watchdog._alive("persona:g1", "persona", root))

    def test_persona_lock_path_matches_runner(self):
        """探测路径必须与 `persona-run` 实际持有的锁**逐字一致**。

        `__main__.py` 里是 `data/shared/persona-<group>.lock`；不一致的话
        watchdog 会以为它没在跑 → 双开 → 同一组两份决策。
        """
        from omnialpha.pidlock import PidLock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "data" / "shared"
            d.mkdir(parents=True, exist_ok=True)
            held = PidLock(d / "persona-eth-disc.lock").acquire()
            self.assertIsNotNone(held)
            try:
                self.assertTrue(Watchdog._alive("persona:eth-disc", "persona", root))
            finally:
                held.release()

    def test_broadcast_lock_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "data" / "shared"
            d.mkdir(parents=True, exist_ok=True)
            self.assertFalse(Watchdog._alive("broadcast", "broadcast", root))
            (d / "broadcast.lock").write_text("", encoding="utf-8")
            self.assertFalse(Watchdog._alive("broadcast", "broadcast", root))

    def test_broadcast_lock_path_matches_runner(self):
        from omnialpha.pidlock import PidLock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "data" / "shared"
            d.mkdir(parents=True, exist_ok=True)
            held = PidLock(d / "broadcast.lock").acquire()
            self.assertIsNotNone(held)
            try:
                self.assertTrue(Watchdog._alive("broadcast", "broadcast", root))
            finally:
                held.release()

    def test_bot_lock_path_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "data" / "bots" / "b1" / "state"
            d.mkdir(parents=True, exist_ok=True)
            self.assertFalse(Watchdog._alive("b1", "plan", root))
            self.assertFalse(Watchdog._alive("b1", "run", root))


class TestSpawnArgs(unittest.TestCase):
    """拉起命令必须与手动运行的方式一致（`persona-run --group` / `broadcast`）。"""

    def _spawn_args(self, target, root):
        import unittest.mock as mock

        wd = Watchdog(root)
        with mock.patch("omnialpha.watchdog.subprocess.Popen") as popen:
            popen.return_value.pid = 1234
            wd._spawn(target)
            return list(popen.call_args[0][0])

    def test_persona_spawn_uses_group_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            args = self._spawn_args(Target("persona:eth-disc", "persona"), root)
            self.assertIn("persona-run", args)
            self.assertEqual(args[args.index("--group") + 1], "eth-disc")
            self.assertNotIn("--bot", args)

    def test_broadcast_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            args = self._spawn_args(Target("broadcast", "broadcast"), Path(td))
            self.assertIn("broadcast", args)
            self.assertNotIn("--bot", args)

    def test_bot_spawn_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            args = self._spawn_args(Target("b1", "plan"), Path(td))
            self.assertEqual(args[args.index("--bot") + 1], "b1")
            self.assertIn("plan-loop", args)

    def test_persona_logs_go_to_shared_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._spawn_args(Target("persona:eth-disc", "persona"), root)
            self.assertTrue((root / "data" / "shared" / "logs").is_dir())


class TestTargetShape(unittest.TestCase):
    def test_component_doc_lists_new_kinds(self):
        t = Target("persona:g1", "persona")
        self.assertEqual(t.bot_id, "persona:g1")
        self.assertEqual(t.restarts, [])
        self.assertFalse(t.stopped)


if __name__ == "__main__":
    unittest.main()

