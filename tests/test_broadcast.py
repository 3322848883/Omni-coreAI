"""信号广播测试：配置/分发/校验/幂等/安全。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.broadcast import (  # noqa: E402
    BroadcastConfig,
    BroadcastError,
    Broadcaster,
    Route,
    load_broadcast_config,
    validate_routes,
)


def _make_tree(root: Path, bots: list[str], signal_name: str = "sig-001.json") -> Path:
    src_inbox = root / "data" / "bots" / bots[0] / "inbox"
    src_inbox.mkdir(parents=True, exist_ok=True)
    (src_inbox / signal_name).write_text(
        json.dumps({"action": "open_long", "symbol": "BTC_USDT", "size_usd": 25}),
        encoding="utf-8",
    )
    return src_inbox / signal_name


class TestConfig(unittest.TestCase):
    def test_load_routes_any_subset(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "broadcast.yaml"
            p.write_text(
                "routes:\n"
                "  - name: one\n    from: feed\n    to: [a]\n"
                "  - name: two\n    from: feed\n    to: [a, b]\n"
                "  - name: many\n    from: feed\n    to: [a, b, c, d]\n",
                encoding="utf-8",
            )
            cfg = load_broadcast_config(p)
            self.assertEqual(len(cfg.routes), 3)
            self.assertEqual(cfg.routes[0].targets, ["a"])
            self.assertEqual(cfg.routes[1].targets, ["a", "b"])
            self.assertEqual(len(cfg.routes[2].targets), 4)

    def test_validate_unknown_target_rejected(self):
        cfg = BroadcastConfig(routes=[Route("r", "feed", ["ghost-bot"])])
        with self.assertRaises(BroadcastError):
            validate_routes(cfg, known_bots={"feed", "real-bot"})

    def test_validate_self_target_rejected(self):
        cfg = BroadcastConfig(routes=[Route("r", "feed", ["feed"])])
        with self.assertRaises(BroadcastError):
            validate_routes(cfg, known_bots={"feed"})

    def test_missing_to_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.yaml"
            p.write_text("routes:\n  - from: feed\n    to: []\n", encoding="utf-8")
            with self.assertRaises(BroadcastError):
                load_broadcast_config(p)


class TestBroadcast(unittest.TestCase):
    def test_fanout_to_multiple(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = _make_tree(root, ["feed", "bot-a", "bot-b"])
            cfg = BroadcastConfig(routes=[Route("r", "feed", ["bot-a", "bot-b"])])
            bc = Broadcaster(root, cfg)
            s = bc.run_once()
            self.assertEqual(s["broadcast"], 1)
            self.assertEqual(s["ok"], 1)
            for b in ("bot-a", "bot-b"):
                dst = root / "data" / "bots" / b / "inbox" / src.name
                self.assertTrue(dst.exists(), b)
                json.loads(dst.read_text(encoding="utf-8"))
            # 源已归档
            self.assertFalse(src.exists())
            arch = root / "data" / "bots" / "feed" / "archive" / "broadcast-done" / src.name
            self.assertTrue(arch.exists())

    def test_partial_failure_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = _make_tree(root, ["feed", "bot-a"])
            cfg = BroadcastConfig(routes=[Route("r", "feed", ["bot-a", "bot-b"])])
            bc = Broadcaster(root, cfg)

            # 让 bot-b 写入失败：把它的 inbox 路径做成文件
            bad = root / "data" / "bots" / "bot-b" / "inbox"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("blocker", encoding="utf-8")

            s = bc.run_once()
            self.assertEqual(s["partial"], 1)
            rec = s["records"][0]
            self.assertEqual(len(rec["delivered"]), 1)
            self.assertEqual(rec["delivered"][0]["bot"], "bot-a")
            self.assertEqual(len(rec["failed"]), 1)
            self.assertEqual(rec["failed"][0]["bot"], "bot-b")

    def test_idempotent_skip_existing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = _make_tree(root, ["feed", "bot-a"])
            cfg = BroadcastConfig(routes=[Route("r", "feed", ["bot-a"])])
            bc = Broadcaster(root, cfg)
            bc.run_once()
            # 重新投同名文件（源已归档，手动造一个同名）
            (root / "data" / "bots" / "feed" / "inbox").mkdir(parents=True, exist_ok=True)
            (root / "data" / "bots" / "feed" / "inbox" / src.name).write_text("{}", encoding="utf-8")
            s = bc.run_once()
            self.assertEqual(s["records"][0]["delivered"][0]["status"], "exists")

    def test_ignores_targets_in_signal(self):
        """AI 在信号里写 targets 不生效（安全）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = root / "data" / "bots" / "feed" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "evil.json").write_text(
                json.dumps({"action": "open_long", "targets": ["attacker-bot"], "exchanges": ["binance"]}),
                encoding="utf-8",
            )
            cfg = BroadcastConfig(routes=[Route("r", "feed", ["bot-a"])])
            bc = Broadcaster(root, cfg)
            s = bc.run_once()
            rec = s["records"][0]
            # 只投了配置里的 bot-a，没有投 attacker-bot
            self.assertEqual(rec["delivered"][0]["bot"], "bot-a")
            self.assertTrue(rec["ignored_targets_in_signal"])
            self.assertFalse((root / "data" / "bots" / "attacker-bot").exists())

    def test_audit_log_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(root, ["feed", "bot-a"])
            cfg = BroadcastConfig(routes=[Route("r", "feed", ["bot-a"])])
            bc = Broadcaster(root, cfg)
            bc.run_once()
            log = root / "data" / "broadcast" / "log.jsonl"
            self.assertTrue(log.exists())
            rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(rec["route"], "r")
            self.assertEqual(rec["delivered"][0]["bot"], "bot-a")


class TestCriticalFixes(unittest.TestCase):
    """Review 抓出的 5 个关键问题的回归测试。"""

    def test_same_source_multi_route_no_starvation(self):
        """同源多路由：一条信号都要投到，不能只投第一条。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = _make_tree(root, ["feed", "a", "b"])
            cfg = BroadcastConfig(routes=[
                Route("r1", "feed", ["a"]),
                Route("r2", "feed", ["b"]),
            ])
            bc = Broadcaster(root, cfg)
            bc.run_once()
            self.assertTrue((root / "data" / "bots" / "a" / "inbox" / src.name).exists())
            self.assertTrue((root / "data" / "bots" / "b" / "inbox" / src.name).exists())

    def test_failed_goes_to_failed_archive(self):
        """任一目标失败 → 源进 broadcast-failed（可重投），不进 done。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = _make_tree(root, ["feed", "bot-a"])
            bad = root / "data" / "bots" / "bot-b" / "inbox"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("blocker", encoding="utf-8")
            cfg = BroadcastConfig(routes=[Route("r", "feed", ["bot-a", "bot-b"])])
            bc = Broadcaster(root, cfg)
            bc.run_once()
            failed_dir = root / "data" / "bots" / "feed" / "archive" / "broadcast-failed"
            done_dir = root / "data" / "bots" / "feed" / "archive" / "broadcast-done"
            self.assertTrue(any(failed_dir.glob(src.name)), "failed archive missing")
            self.assertFalse(any(done_dir.glob(src.name)), "should not be in done")

    def test_partial_counts_as_failure_exit(self):
        """partial 属失败：退出码应为 1。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(root, ["feed", "bot-a"])
            bad = root / "data" / "bots" / "bot-b" / "inbox"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("x", encoding="utf-8")
            cfg = BroadcastConfig(routes=[Route("r", "feed", ["bot-a", "bot-b"])])
            bc = Broadcaster(root, cfg)
            s = bc.run_once()
            self.assertEqual(s["failed"], 0)
            self.assertEqual(s["partial"], 1)
            exit_code = 0 if (s.get("failed", 0) == 0 and s.get("partial", 0) == 0) else 1
            self.assertEqual(exit_code, 1)

    def test_path_traversal_rejected(self):
        """bot_id 含 ../ 或路径分隔符 → 拒绝。"""
        for evil in ("../x", "..\\x", "a/b", ".."):
            cfg = BroadcastConfig(routes=[Route("r", "feed", [evil])])
            with self.assertRaises(BroadcastError):
                validate_routes(cfg, known_bots={"feed", "x", "a"})

    def test_source_not_found_rejected(self):
        """from 的 bot 不存在 → 拒绝启动（不静默 no-op）。"""
        cfg = BroadcastConfig(routes=[Route("r", "ghost", ["bot-a"])])
        with self.assertRaises(BroadcastError):
            validate_routes(cfg, known_bots={"bot-a"})

    def test_config_missing_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(BroadcastError):
                load_broadcast_config(Path(td) / "nope.yaml")


if __name__ == "__main__":
    unittest.main()
