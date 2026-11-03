# -*- coding: utf-8 -*-
"""底仓触发过滤单测：_base_pos_st_filter 的四个象限 + db 缺数据兜底。

用临时 SQLite 喂 4h OHLC，构造 EMA55/ST 方向可控的序列。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.strategist.indicators import attach_indicators  # noqa: E402


def _mk_db(db_path: Path, bars: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS kline (t INTEGER, o REAL, h REAL,"
                " l REAL, c REAL, v REAL, symbol TEXT, interval TEXT, closed INTEGER)")
    con.executemany(
        "INSERT INTO kline (t,o,h,l,c,v,symbol,interval,closed) VALUES (?,?,?,?,?,?,?,?,1)",
        [(b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v", 10.0),
          "ETH_USDT", "4h", ) for b in bars])
    con.commit()
    con.close()


def _series(dir_: int, n: int = 120, base: float = 2400.0) -> list[dict]:
    """构造 ST dir 恒为 dir_ 的 4h 序列：dir=1（跌）时价格持续下移。

    supertrend10 的 dir 由价格与 ST 线关系决定 —— 单调走势可稳定产生固定 dir。
    """
    out = []
    px = base
    t0 = 1_700_000_000
    for i in range(n):
        if dir_ == 1:
            o = px
            c = px - 6 - (i % 3)          # 持续下移
            h = o + 2
            l = c - 2
            px = c
        else:
            o = px
            c = px + 6 + (i % 3)          # 持续上移
            h = c + 2
            l = o - 2
            px = c
        out.append({"t": t0 + i * 14400, "o": o, "h": h, "l": l, "c": c})
    return out


class TestBasePosSTFilter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "kline.db"
        self.ex = Executor.__new__(Executor)          # 不走 __init__
        self.ex.ladder_template = {"levels_dynamic": {"db_path": str(self.db)}}

    def _filter(self, side, mark, ema_len=55):
        return self.ex._base_pos_st_filter(
            "ETH_USDT", side, {"ema_len": ema_len}, mark)

    def test_down_trend_short_high_ok(self):
        """4H ST 跌（dir=1）+ 现价 ≥ EMA55（反弹到高位）→ 空头底仓 OK。"""
        bars = _series(1, 120, 2600.0)             # 跌势：EMA55 滞后在上方
        _mk_db(self.db, bars)
        _, note = self._filter("short", 99999.0)   # 先探测 EMA 位置
        ema = note["ema"]
        ok, note = self._filter("short", ema + 20)  # mark 在 EMA 上方（高位）
        self.assertTrue(ok, note)
        self.assertTrue(note["dir_ok"] and note["ema_ok"])

    def test_down_trend_short_below_ema_reject(self):
        """4H ST 跌 + 现价 < EMA55（跌得太深）→ 空头底仓拒绝（不在高位）。"""
        bars = _series(1, 120, 2600.0)
        _mk_db(self.db, bars)
        _, note = self._filter("short", 99999.0)
        ema = note["ema"]
        ok, note = self._filter("short", ema - 60)  # mark 远低于 EMA（跌太深）
        self.assertFalse(ok)
        self.assertFalse(note["ema_ok"])

    def test_up_trend_long_low_ok(self):
        """4H ST 涨（dir=-1）+ 现价 ≤ EMA55（回调到低位）→ 多头底仓 OK。"""
        bars = _series(-1, 120, 2200.0)            # 涨势：EMA55 滞后在下方
        _mk_db(self.db, bars)
        _, note = self._filter("long", 0.0001)      # 先探测 EMA 位置
        ema = note["ema"]
        ok, note = self._filter("long", ema - 20)   # mark 在 EMA 下方（低位）
        self.assertTrue(ok, note)

    def test_up_trend_long_above_ema_reject(self):
        """4H ST 涨 + 现价 > EMA55（涨得太高）→ 多头底仓拒绝（不在低位）。"""
        bars = _series(-1, 120, 2200.0)
        _mk_db(self.db, bars)
        _, note = self._filter("long", 0.0001)
        ema = note["ema"]
        ok, note = self._filter("long", ema + 60)   # mark 远高于 EMA（涨太高）
        self.assertFalse(ok)
        self.assertFalse(note["ema_ok"])

    def test_direction_mismatch_reject(self):
        """方向不一致：跌势里建多头底仓 → 拒（无论价位）。"""
        bars = _series(1, 120, 2600.0)
        _mk_db(self.db, bars)
        ok, note = self._filter("long", bars[-1]["c"] - 50)
        self.assertFalse(ok)
        self.assertFalse(note["dir_ok"])

    def test_db_missing_fail_safe(self):
        """db 不存在 → fail-safe 拒绝（不建底仓）。"""
        ok, note = self._filter("short", 2500.0)
        self.assertFalse(ok)
        self.assertIn("error", note)

    def test_state_not_written_on_filter_reject(self):
        """过滤拒绝的轮次不写 base_position state（下轮条件满足可再试）。"""
        bars = _series(1, 120, 2600.0)
        _mk_db(self.db, bars)
        # 模拟接入点语义：过滤 False → _bp_px=0 → 不写 state（这里验证 helper 层）
        ok, _ = self._filter("short", bars[-1]["c"] - 50)
        self.assertFalse(ok)
        st_file = Path(self.tmp) / "state.json"
        self.assertFalse(st_file.exists())


if __name__ == "__main__":
    unittest.main()
