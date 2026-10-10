"""Chip 契约 Tier 1：7 个 optional 字段 + 区间校验（S2）。

设计要点（见 docs/compose/spec/pa-skills-upgrade.md [S2]）：
- 7 个字段全部 optional，缺省不改变任何现有行为（对正在跑的 bot 零影响）；
- `region == "range"` 时不得给 `tp2` —— 把提示词「区域=区间 → 禁止持有 2R 目标」
  从一句话变成程序约束。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.schema import (  # noqa: E402
    Chip,
    PlanError,
    extract_kline_reads,
    parse_plan,
)


def _chip(**over) -> dict:
    base = {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.7, "size_usd": 100}
    base.update(over)
    return base


class TestTier1Fields(unittest.TestCase):
    def test_new_fields_parsed(self):
        plan = parse_plan({"chips": [_chip(
            region="trend",
            invalidation=85560.0,
            time_stop_bars=8,
            give_back_pct=40.0,
            risk_pct=1.4,
            rule_ids=["SB-06", "SA-05"],
            scenarios={"entry_pending": "hold", "position_open": "trail"},
        )]})
        c = plan.chips[0]
        self.assertEqual(c.region, "trend")
        self.assertEqual(c.invalidation, 85560.0)
        self.assertEqual(c.time_stop_bars, 8)
        self.assertEqual(c.give_back_pct, 40.0)
        self.assertEqual(c.risk_pct, 1.4)
        self.assertEqual(c.rule_ids, ["SB-06", "SA-05"])
        self.assertEqual(c.scenarios["entry_pending"], "hold")

    def test_new_fields_passed_through_to_signal(self):
        plan = parse_plan({"chips": [_chip(
            region="trend", invalidation=85560.0, rule_ids=["SB-06"], risk_pct=1.4,
        )]})
        d = plan.chips[0].to_signal_dict()
        self.assertEqual(d["region"], "trend")
        self.assertEqual(d["invalidation"], 85560.0)
        self.assertEqual(d["rule_ids"], ["SB-06"])
        self.assertEqual(d["risk_pct"], 1.4)

    def test_absent_fields_keep_old_behavior(self):
        """旧 Plan JSON（无新字段）解析与透传都不变。"""
        plan = parse_plan({"chips": [_chip(tp=86000.0, sl=85500.0)]})
        c = plan.chips[0]
        self.assertEqual(c.region, "")
        self.assertIsNone(c.invalidation)
        self.assertIsNone(c.time_stop_bars)
        self.assertIsNone(c.give_back_pct)
        self.assertIsNone(c.risk_pct)
        self.assertEqual(c.rule_ids, [])
        self.assertEqual(c.scenarios, {})
        d = c.to_signal_dict()
        for k in ("region", "invalidation", "time_stop_bars", "give_back_pct",
                  "risk_pct", "rule_ids", "scenarios"):
            self.assertNotIn(k, d, f"未设置时不应透传 {k}")

    def test_invalid_region_rejected(self):
        with self.assertRaises(PlanError) as cm:
            parse_plan({"chips": [_chip(region="up")]})
        self.assertIn("region", str(cm.exception))


class TestRangeForbidsTp2(unittest.TestCase):
    def test_range_with_tp2_rejected(self):
        with self.assertRaises(PlanError) as cm:
            parse_plan({"chips": [_chip(region="range", tp=86150.0, tp2=86600.0)]})
        self.assertIn("tp2", str(cm.exception))

    def test_range_without_tp2_ok(self):
        plan = parse_plan({"chips": [_chip(region="range", tp=86150.0, sl=85560.0)]})
        self.assertEqual(plan.chips[0].region, "range")
        self.assertIsNone(plan.chips[0].tp2)

    def test_trend_with_tp2_ok(self):
        plan = parse_plan({"chips": [_chip(region="trend", tp=86150.0, tp2=86600.0)]})
        self.assertEqual(plan.chips[0].tp2, 86600.0)

    def test_absent_region_with_tp2_ok(self):
        """未声明区域时不做校验 —— 保持向后兼容。"""
        plan = parse_plan({"chips": [_chip(tp=86150.0, tp2=86600.0)]})
        self.assertEqual(plan.chips[0].tp2, 86600.0)

    def test_reversal_with_tp2_ok(self):
        plan = parse_plan({"chips": [_chip(region="reversal", tp=86150.0, tp2=86600.0)]})
        self.assertEqual(plan.chips[0].region, "reversal")


class TestChipDefaults(unittest.TestCase):
    def test_dataclass_defaults(self):
        c = Chip(symbol="BTC_USDT", action="hold")
        self.assertEqual(c.region, "")
        self.assertIsNone(c.invalidation)
        self.assertIsNone(c.time_stop_bars)
        self.assertIsNone(c.give_back_pct)
        self.assertIsNone(c.risk_pct)
        self.assertEqual(c.rule_ids, [])
        self.assertEqual(c.scenarios, {})


class TestBadInputRaisesPlanError(unittest.TestCase):
    """坏输入必须抛 `PlanError`，不能漏出 `ValueError`/`TypeError`。

    实测动因（独立评审 Critical 2）：调用方**只捕 `PlanError`**（`loop.py` 的
    `except PlanError`、`__main__.cmd_plan` 连 try 都没有）。直转 `int()`/`float()`
    会把 `ValueError` 漏出去 → `plan` 命令 traceback；`plan-loop` 虽被外层兜住，
    但会**绕过 `degraded` 与 `_record_cycle_failure`** → `plan_fail` 告警静默不计数。

    暴露面是**新增**的：改动前 `time_stop_bars` 这类字段根本不被解析（写什么都忽略），
    而规则 18 现在要求模型填 `give_back_pct`/`risk_pct`（语义带 `(%)`，写 `"40%"` 即命中）。
    """

    def test_int_field_with_suffix(self):
        with self.assertRaises(PlanError):
            parse_plan({"chips": [_chip(time_stop_bars="8bars")]})

    def test_int_field_with_list(self):
        with self.assertRaises(PlanError):
            parse_plan({"chips": [_chip(time_stop_bars=[])]})

    def test_float_field_with_text(self):
        with self.assertRaises(PlanError):
            parse_plan({"chips": [_chip(risk_pct="abc")]})

    def test_float_field_with_percent_sign(self):
        """规则 18 把 `give_back_pct` 描述成「浮盈回撤阈值(%)」—— 模型可能写 `40%`。"""
        with self.assertRaises(PlanError):
            parse_plan({"chips": [_chip(give_back_pct="40%")]})

    def test_existing_float_fields_also_guarded(self):
        """老字段（tp/sl/size_usd…）此前也会漏 ValueError，一并收口。"""
        for field in ("tp", "sl", "tp2", "price", "size_usd"):
            with self.subTest(field=field):
                with self.assertRaises(PlanError):
                    parse_plan({"chips": [_chip(**{field: "not-a-number"})]})


class TestKlineRead(unittest.TestCase):
    """逐K形态读（`kline_tf` / `kline_read`）—— 人格可选产出，见 prompts/brooks_btc_pa.md。

    与 Tier 1 那 7 个字段的**关键区别**：这两个字段刻意**不进 `to_signal_dict`**。
    每轮约 1.2KB，进了订单上下文就会随下一轮注入，污染 prompt 与缓存前缀。

    背景（2026-10-07 实测）：人格里「逐K分析最近20根（强制）」与契约规则 7
    「reasoning 必须 ≤30 字」直接冲突，而 schema 里没有承载它的字段 —— 150 轮
    里 11.3% 声称做过、0% 真逐根列举。给它一个字段后，本地 8/8 轮填满 20 条。
    """

    def test_parsed_when_present(self):
        plan = parse_plan({"chips": [_chip(
            kline_tf="15m",
            kline_read=["18:45 O83286.2 H83369.1 L83280.1 C83355.1 小阳线",
                        "18:30 O83328.0 H83349.1 L83260.3 C83286.3 内包K"],
        )]})
        c = plan.chips[0]
        self.assertEqual(c.kline_tf, "15m")
        self.assertEqual(len(c.kline_read), 2)

    def test_defaults_empty_when_absent(self):
        """没被要求这个字段的 bot 不填 → 空，行为零变化。"""
        c = parse_plan({"chips": [_chip()]}).chips[0]
        self.assertEqual(c.kline_tf, "")
        self.assertEqual(c.kline_read, [])

    def test_not_exported_to_signal(self):
        """**关键回归**：不能进订单上下文（会污染下一轮 prompt 与缓存前缀）。"""
        d = parse_plan({"chips": [_chip(kline_tf="5m", kline_read=["b1"])]}).chips[0].to_signal_dict()
        self.assertNotIn("kline_tf", d)
        self.assertNotIn("kline_read", d)
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("kline_tf", blob)
        self.assertNotIn("kline_read", blob)

    def test_bad_shape_does_not_fail_the_plan(self):
        """观测性字段：格式坏掉不该让整轮降级成 hold（对照 `memory_refs` 的先例）。"""
        c = parse_plan({"chips": [_chip(kline_read="not-a-list")]}).chips[0]
        self.assertEqual(c.kline_read, [])
        c2 = parse_plan({"chips": [_chip(kline_read=[1, None, "ok", "  ", "b"])]}).chips[0]
        self.assertEqual(c2.kline_read, ["ok", "b"])

    def test_capped_at_30(self):
        c = parse_plan({"chips": [_chip(kline_read=[f"bar{i}" for i in range(50)])]}).chips[0]
        self.assertEqual(len(c.kline_read), 30)


class TestExtractKlineReads(unittest.TestCase):
    """`_save_thinking` 用的轻量抽取 —— 它在 `parse_plan_text` **之前**调用。"""

    def test_extracts_from_fenced_json(self):
        text = '```json\n{"chips":[{"kline_tf":"5m","kline_read":["b1","b2"]}]}\n```'
        got = extract_kline_reads(text)
        self.assertEqual(got["kline_tf"], "5m")
        self.assertEqual(got["kline_read"], ["b1", "b2"])

    def test_empty_when_absent(self):
        self.assertEqual(extract_kline_reads('{"chips":[{"symbol":"BTC_USDT"}]}'), {})

    def test_empty_on_garbage(self):
        for bad in ("not json at all", "", "   ", "{broken"):
            with self.subTest(bad=bad):
                self.assertEqual(extract_kline_reads(bad), {})


class TestSaveThinkingKline(unittest.TestCase):
    """落盘侧：**非空才写** —— 其他 bot 的 thinking.json 与改动前逐字节一致。"""

    def _runner(self, td: Path):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        return PlanRunner(
            client=object(),
            cfg=StrategistConfig(symbols=["BTC_USDT"]),
            inbox=td / "inbox", history_dir=td / "hist", llm=object(),
        )

    def _read(self, td: Path) -> dict:
        files = list((td / "hist").glob("*.thinking.json"))
        self.assertEqual(len(files), 1, f"应恰好落一个 thinking.json: {files}")
        return json.loads(files[0].read_text(encoding="utf-8"))

    def test_written_when_present(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            self._runner(td)._save_thinking(
                cycle_id="c1", trigger="t",
                content='{"chips":[{"kline_tf":"5m","kline_read":["b1","b2"]}]}',
                reasoning=["cot"])
            payload = self._read(td)
            self.assertEqual(payload["kline"]["kline_tf"], "5m")
            self.assertEqual(payload["kline"]["kline_read"], ["b1", "b2"])

    def test_absent_key_when_not_emitted(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            self._runner(td)._save_thinking(
                cycle_id="c1", trigger="t",
                content='{"chips":[{"symbol":"BTC_USDT","action":"hold"}]}',
                reasoning=["cot"])
            self.assertNotIn("kline", self._read(td))

    def test_symbol_recorded_and_multi_becomes_array(self):
        """D-27：审计体要能回答「这轮给模型发了哪几个币的逐K读」。

        原先只留**首个**有内容的 chip → 其余币的逐K读凭空消失，且不带 symbol，
        按币审计根本无从下手。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            self._runner(td)._save_thinking(
                cycle_id="c1", trigger="t",
                content=('{"chips":['
                         '{"symbol":"BTC_USDT","kline_tf":"5m","kline_read":["b1"]},'
                         '{"symbol":"ETH_USDT","kline_tf":"15m","kline_read":["e1","e2"]}]}'),
                reasoning=["cot"])
            k = self._read(td)["kline"]
            self.assertEqual(k["symbol"], "BTC_USDT", "旧字段仍要能读到（向后兼容）")
            self.assertEqual(k["kline_read"], ["b1"])
            self.assertEqual([e["symbol"] for e in k["kline_reads"]],
                             ["BTC_USDT", "ETH_USDT"], "多币必须落数组")
            self.assertEqual(k["kline_reads"][1]["kline_read"], ["e1", "e2"])

    def test_single_chip_keeps_flat_shape(self):
        """单 chip 不加数组 —— 单币 bot 的 thinking.json 形状不变（I11）。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            self._runner(td)._save_thinking(
                cycle_id="c1", trigger="t",
                content='{"chips":[{"symbol":"BTC_USDT","kline_tf":"5m","kline_read":["b1"]}]}',
                reasoning=["cot"])
            self.assertNotIn("kline_reads", self._read(td)["kline"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
