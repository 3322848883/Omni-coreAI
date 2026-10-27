#!/usr/bin/env python3
"""修 ohlc_sample 门禁：排除正在形成的当前 K 线（否则活库写入 → 假阳性）。"""
import pathlib

p = pathlib.Path("pa-data-source/migrate_v2.py")
s = p.read_text(encoding="utf-8")

old = '''    for symbol, interval in conn.execute(
            "SELECT DISTINCT symbol, interval FROM kline").fetchall():
        src_rows = conn.execute(
            "SELECT t,o,h,l,c FROM kline WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT 20",
            (symbol, interval)).fetchall()'''
new = '''    for symbol, interval in conn.execute(
            "SELECT DISTINCT symbol, interval FROM kline").fetchall():
        # ⚠️ 只比**已收盘**的 bar：`ORDER BY t DESC LIMIT 20` 会把正在形成的那根
        # 也算进去，而活库里它的 OHLC 每几秒变一次 —— 复制快照与比对时刻不同 →
        # 必然不等。实测 2026-10-28：同一份数据两次 dry-run 分别报 1.8e-4 / 9.9e-5 / 0.0，
        # 全是「当前 bar 在动」造成的假阳性。取 21 根、跳过最新那根。
        src_rows = conn.execute(
            "SELECT t,o,h,l,c FROM kline WHERE symbol=? AND interval=? "
            "ORDER BY t DESC LIMIT 21",
            (symbol, interval)).fetchall()
        if len(src_rows) > 20:
            src_rows = src_rows[1:]      # 丢掉未收盘的最新 bar'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")
print("ohlc gate fixed")
