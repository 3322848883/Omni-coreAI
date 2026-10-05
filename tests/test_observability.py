"""可观测性：统一健康视图 + 完整留痕。

回归背景（2026-10-05 架构盘点 S20/S21）：
- `thinking.json` 的 `content_head` 截 500 字符 → 长 Plan 被腰斩，事后核对
  「模型到底输出了什么」看不到尾部
- **讨论轮的 LLM 调用完全不落盘** → 只有截断到 30 字的 reasoning 进
  `discussion_log`，无法核对「模型看到什么同伴观点、回了什么」
- 健康信息散在 `health.json` / `health.run.json` / `alerts.json` /
  `bots.db` 四处，`status` 完全不暴露健康
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestThinkingNotTruncated(unittest.TestCase):
    """`_save_thinking` 的正文不再腰斩（原先截 500 字符）。"""

    def test_long_content_survives(self):
        from omnialpha.strategist.loop import PlanRunner

        with tempfile.TemporaryDirectory() as td:
            runner = PlanRunner.__new__(PlanRunner)
            runner.history_dir = Path(td) / "state"
            runner.tool_usage = []
            runner.ledger = None
            long_plan = "x" * 3000
            runner._save_thinking(cycle_id="c-1", trigger="interval",
                                  reasoning=["r"], content=long_plan)
            files = list(runner.history_dir.glob("*.thinking.json"))
            self.assertEqual(len(files), 1)
            rec = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(len(rec["content_head"]), 3000,
                             "正文不该被截断到 500")


class TestDiscussionCallLogging(unittest.TestCase):
    """讨论轮的 LLM 调用必须落盘（原先完全不落）。"""

    def test_writes_jsonl(self):
        from omnialpha.strategist.loop import PlanRunner

        with tempfile.TemporaryDirectory() as td:
            runner = PlanRunner.__new__(PlanRunner)
            runner.history_dir = Path(td) / "state"
            runner._log_discussion_call(2, "SYS", "USER", '{"decision":"hold"}')
            p = runner.history_dir / "discussion_calls.jsonl"
            self.assertTrue(p.exists())
            rec = json.loads(p.read_text(encoding="utf-8").strip())
            self.assertEqual(rec["round"], 2)
            self.assertEqual(rec["system"], "SYS")
            self.assertEqual(rec["user"], "USER")
            self.assertIn("hold", rec["raw"])

    def test_appends_multiple_rounds(self):
        from omnialpha.strategist.loop import PlanRunner

        with tempfile.TemporaryDirectory() as td:
            runner = PlanRunner.__new__(PlanRunner)
            runner.history_dir = Path(td) / "state"
            for r in (1, 2, 3):
                runner._log_discussion_call(r, "S", "U", f"raw{r}")
            lines = (runner.history_dir / "discussion_calls.jsonl").read_text(
                encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual([json.loads(x)["round"] for x in lines], [1, 2, 3])

    def test_failure_is_silent(self):
        """落盘失败不能影响讨论主流程。"""
        from omnialpha.strategist.loop import PlanRunner

        runner = PlanRunner.__new__(PlanRunner)
        runner.history_dir = Path("/nonexistent/\x00bad")
        runner._log_discussion_call(1, "S", "U", "r")  # 不该抛


class TestHealthCommand(unittest.TestCase):
    def test_health_outputs_json(self):
        import contextlib
        import io

        from omnialpha.__main__ import cmd_health

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config" / "bots").mkdir(parents=True)
            (root / "config" / "bots" / "b1.yaml").write_text(
                "bot_id: b1\nenv: live\nsymbols: [BTC_USDT]\n", encoding="utf-8")
            args = type("A", (), {"root": str(root), "bot": ""})()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_health(args)
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertIn("bots", data)
            self.assertEqual(data["bots"][0]["bot_id"], "b1")
            self.assertIn("fail_streak", data["bots"][0])
            self.assertIn("health_check", data["bots"][0])


if __name__ == "__main__":
    unittest.main()
