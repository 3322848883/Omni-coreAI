# -*- coding: utf-8 -*-
"""AI 触发器幂等去重：同 (type, symbol, params) 不重复创建。

为什么必须去重（线上实测 2026-10-02 实盘 brooks-btc）：
5 个条件里 4 个是同质 `price_break` on BTC_USDT，每个各按自己的 cooldown 唤醒
一次 → 聚合唤醒频率被抬高到约 1 分钟一轮（每轮一次 LLM 调用），而正常节奏是
15 分钟一轮。同质条件既不占新槽位、也不该叠加唤醒次数。

AI 侧已给它可见性（prompt 里列出生效触发器 + 参数范围），这里是 store 层兜底。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from omnialpha.strategist.trigger_store import AITriggerPolicy, AITriggerStore


def _norm(ctype="price_break", symbol="BTC_USDT", **params) -> dict:
    return {"type": ctype, "symbol": symbol, "params": dict(params)}


class TestTriggerDedup(unittest.TestCase):
    def _store(self, td: str, max_active: int = 5) -> AITriggerStore:
        pol = AITriggerPolicy(enabled=True, max_active=max_active,
                              default_cooldown_sec=300.0)
        return AITriggerStore(Path(td) / "ai_triggers.json", pol)

    def test_same_trigger_returns_existing(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            a = s.add(_norm(lookback=30, side="high"))
            b = s.add(_norm(lookback=30, side="high"))
            self.assertEqual(a.id, b.id, "同质条件应返回同一个，而不是新建")
            self.assertEqual(len(s.active()), 1)

    def test_param_order_does_not_matter(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            a = s.add(_norm(lookback=30, side="high"))
            b = s.add(_norm(side="high", lookback=30))
            self.assertEqual(a.id, b.id)

    def test_int_float_equivalent(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            a = s.add(_norm(lookback=30, side="high"))
            b = s.add(_norm(lookback=30.0, side="high"))
            self.assertEqual(a.id, b.id)

    def test_different_params_create_new(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            s.add(_norm(lookback=30, side="high"))
            s.add(_norm(lookback=40, side="high"))
            self.assertEqual(len(s.active()), 2)

    def test_different_side_creates_new(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            s.add(_norm(lookback=30, side="high"))
            s.add(_norm(lookback=30, side="low"))
            self.assertEqual(len(s.active()), 2)

    def test_different_type_creates_new(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            s.add(_norm(lookback=30, side="high"))
            s.add(_norm(ctype="rsi", period=14, level=70, op="gt"))
            self.assertEqual(len(s.active()), 2)

    def test_different_symbol_creates_new(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            s.add(_norm(lookback=30, side="high"))
            s.add(_norm(symbol="ETH_USDT", lookback=30, side="high"))
            self.assertEqual(len(s.active()), 2)

    def test_dup_does_not_consume_slot(self):
        """槽位满时再发同质条件应成功（返回已有的），而不是被 trigger_limit 拒。"""
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td, max_active=2)
            s.add(_norm(lookback=30, side="high"))
            s.add(_norm(lookback=40, side="high"))
            # 满了
            with self.assertRaises(Exception):
                s.add(_norm(lookback=50, side="high"))
            # 但重复已存在的不该被拒
            same = s.add(_norm(lookback=30, side="high"))
            self.assertTrue(same.id)
            self.assertEqual(len(s.active()), 2)

    def test_replace_all_dedups(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            out = s.replace_all([
                _norm(lookback=30, side="high"),
                _norm(lookback=30, side="high"),
                _norm(lookback=40, side="high"),
            ])
            self.assertEqual(len(s.active()), 2)
            self.assertEqual(out[0].id, out[1].id)

    def test_dup_does_not_refresh_ttl(self):
        """重复添加不刷新 TTL —— 否则条件永不失效，TTL 失去清理意义。"""
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td)
            a = s.add(_norm(lookback=30, side="high"))
            first_expire = a.expire_at
            time.sleep(0.01)
            b = s.add(_norm(lookback=30, side="high"))
            self.assertEqual(b.expire_at, first_expire)

    def test_dedup_survives_reload(self):
        """落盘后重新加载，去重仍生效（每轮新建实例）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ai_triggers.json"
            pol = AITriggerPolicy(enabled=True, max_active=5)
            s1 = AITriggerStore(p, pol)
            a = s1.add(_norm(lookback=30, side="high"))
            s2 = AITriggerStore(p, pol)
            b = s2.add(_norm(lookback=30, side="high"))
            self.assertEqual(a.id, b.id)
            self.assertEqual(len(s2.active()), 1)


if __name__ == "__main__":
    unittest.main()
