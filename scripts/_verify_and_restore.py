#!/usr/bin/env python3
"""验证 v2 表内容 + 恢复 kline 服务（服务当前是停的，必须马上起）。"""
import sqlite3
import subprocess

con = sqlite3.connect("file:data/kline.db?mode=ro", uri=True)
tabs = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("tables:", tabs)
print("rows:", con.execute("SELECT count(*) FROM kline").fetchone()[0])

cols = [r[1] for r in con.execute("PRAGMA table_info(kline)")]
print("kline 列数:", len(cols))
print("指标列:", [c for c in cols if c not in ("t", "o", "h", "l", "c", "v", "sum", "symbol", "interval", "closed")])

print("\n最新一行（SOL 15m）：")
row = con.execute(
    "SELECT t, o, c, ema20, ema200, atr14, rsi14, boll20, boll20_upper, boll20_lower "
    "FROM kline WHERE symbol='SOL_USDT' AND interval='15m' ORDER BY t DESC LIMIT 1").fetchone()
print(" ", row)

if "kline_meta" in tabs:
    print("\nmeta:")
    for k, v in con.execute("SELECT key, value FROM kline_meta"):
        print(f"  {k} = {v}")

print("\n每序列指标 NULL 抽查（应全 0）：")
bad = 0
for sym, iv in con.execute("SELECT DISTINCT symbol, interval FROM kline").fetchall():
    n = con.execute(
        "SELECT count(*) FROM kline WHERE symbol=? AND interval=? AND closed=1 "
        "AND (ema20 IS NULL OR ema200 IS NULL OR atr14 IS NULL OR boll20 IS NULL "
        "OR rsi14 IS NULL)", (sym, iv)).fetchone()[0]
    if n:
        bad += 1
        print(f"  {sym} {iv}: {n}")
print("  有 NULL 的序列数:", bad)
con.close()

print("\n=== 恢复服务 ===")
print(subprocess.run("systemctl start omnialpha-kline-live && sleep 8 && "
                     "systemctl is-active omnialpha-kline-live",
                     shell=True, capture_output=True, text=True).stdout.strip())
