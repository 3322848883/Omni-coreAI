"""导出 ETH 多周期行情到 skill 数据目录，跑 skill 自带的市场状态脚本。"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "price-action-trading"
DATA = SKILL / "data"
DATA.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(ROOT / "pa-data-source" / "data" / "kline.db")
for tf, n in (("4h", 200), ("1h", 200), ("15m", 200), ("5m", 200)):
    rows = con.execute(
        "SELECT t,o,h,l,c,v,ema20,atr14 FROM kline WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?",
        ("ETH_USDT", tf, n),
    ).fetchall()
    rows.reverse()
    out = []
    for t, o, h, l, c, v, ema20, atr14 in rows:
        out.append({
            "t": t, "o": o, "h": h, "l": l, "c": c, "v": v,
            "ema20": ema20, "atr14": atr14,
        })
    p = DATA / f"eth_usdt_{tf}.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"{tf}: {len(out)} bars -> {p.name}  last={out[-1]['c'] if out else None}")
con.close()

print("\n=== skill analyze_market_state.py eth 4h,1h,5m ===")
r = subprocess.run(
    [sys.executable, str(SKILL / "scripts" / "analyze_market_state.py"), "eth", "4h,1h,5m"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    cwd=str(SKILL),
    env={**__import__("os").environ, "PRICE_ACTION_SKILL_ROOT": str(SKILL)},
)
print(r.stdout)
if r.stderr:
    print("STDERR:", r.stderr[:500])
