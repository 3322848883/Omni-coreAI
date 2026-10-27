#!/usr/bin/env python3
"""数据源升级现状核实：引擎模式 / 库表 / watchlist / 影子统计 / 服务。"""
import json
import os
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path("/opt/omnialpha")
DS = ROOT / "pa-data-source"


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"ERR {e}"


print("=== ① 服务与引擎模式 ===")
print(sh("systemctl show -p Environment omnialpha-kline-live | tr ' ' '\\n' | grep -i PA_ || echo '(无 PA_ 环境变量 = legacy)'"))
print("unit:", sh("systemctl is-active omnialpha-kline-live"),
      "| 启动:", sh("ps -o lstart= -p $(systemctl show -p MainPID --value omnialpha-kline-live) 2>/dev/null"))

print("\n=== ② 库与表 ===")
for db in sorted(DS.glob("data/*.db")) + sorted(DS.glob("data/*/*.db")):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        tabs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        sizes = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tabs}
        con.close()
        print(f"  {db.name}: " + ", ".join(f"{k}={v}" for k, v in sizes.items()))
    except Exception as e:  # noqa: BLE001
        print(f"  {db}: ERR {e}")

print("\n=== ③ watchlist（指标声明）===")
for p in list(DS.glob("*.yaml")) + list(DS.glob("config/*.yaml")):
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if "watchlist" in txt or "indicators" in txt:
        print(f"  {p.relative_to(DS)}:")
        for ln in txt.splitlines():
            if any(k in ln for k in ("symbol", "interval", "indicator", "engine", "dataset")):
                print("   ", ln.strip()[:110])

print("\n=== ④ 影子/迁移痕迹 ===")
print(sh("ls -la /opt/omnialpha/pa-data-source/data/*.db 2>/dev/null | awk '{print $5, $9}'"))
print(sh("ls /opt/omnialpha/pa-data-source/data/shadow* /opt/omnialpha/pa-data-source/reports/*engine* 2>/dev/null | head -5"))
