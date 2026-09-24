"""CLI/MCP 备用数据源（backup_source.py）

主数据源（WebSocket/REST）异常时自动切换到 gate-cli 兜底拉取 K 线。
判定规则：某 (symbol, interval) 连续 3 次收到无效 K 线（OHLC 缺失或全 0）→ 判定主源异常，
激活备用源，用 gate-cli 拉取该时间戳 K 线替换；收到有效 K 线后自动恢复主源。

被 kline_watcher.py 引用，接口契约：
- check_and_switch_source(symbol, interval, candle, market_type) -> dict | None
- get_backup_status() -> {key: active}
- is_backup_active() -> bool
- restore_primary_source(symbol=None, interval=None) -> None
"""

import json
import os
import subprocess
import sys
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GATE_CLI = os.path.join(SCRIPT_DIR, "gate-cli.exe" if sys.platform == "win32" else "gate-cli")

FAIL_THRESHOLD = 3

_lock = threading.Lock()
# {key: {"fail_count": int, "active": bool}}
_backup_state = {}


def _key(symbol, interval):
    return f"{symbol}_{interval}"


def _is_valid_candle(candle):
    """K 线有效：OHLC 字段存在且不全为 0"""
    if not isinstance(candle, dict):
        return False
    try:
        o = float(candle.get("o", "0"))
        h = float(candle.get("h", "0"))
        l = float(candle.get("l", "0"))
        c = float(candle.get("c", "0"))
    except (TypeError, ValueError):
        return False
    return o > 0 and h > 0 and l > 0 and c > 0


def _run_cli(args, timeout=15):
    cmd = [GATE_CLI] + args + ["--format", "json"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=SCRIPT_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _fetch_candle_from_cli(symbol, interval, t, market_type):
    """用 gate-cli 拉取指定时间戳的 K 线，返回与 kline_watcher 一致的 candle 结构"""
    if market_type == "spot":
        data = _run_cli(
            ["info", "marketdetail", "get-kline", "--symbol", symbol, "--timeframe", interval, "--limit", "5"]
        )
        if not isinstance(data, dict):
            return None
        for row in data.get("items", []):
            if int(row.get("t", 0)) == int(t):
                return {
                    "t": int(row["t"]),
                    "o": str(row.get("open", "0")),
                    "h": str(row.get("high", "0")),
                    "l": str(row.get("low", "0")),
                    "c": str(row.get("close", "0")),
                    "v": str(row.get("volume", "0")),
                    "sum": str(row.get("quote_volume", "0")),
                }
        return None
    data = _run_cli(
        ["cex", "futures", "market", "candlesticks", "--contract", symbol, "--interval", interval, "--limit", "5"]
    )
    if not isinstance(data, list):
        return None
    for row in data:
        if int(row.get("t", 0)) == int(t):
            return {
                "t": int(row["t"]),
                "o": str(row.get("o", "0")),
                "h": str(row.get("h", "0")),
                "l": str(row.get("l", "0")),
                "c": str(row.get("c", "0")),
                "v": str(row.get("v", "0")),
                "sum": str(row.get("sum", "0")),
            }
    return None


def check_and_switch_source(symbol, interval, candle, market_type):
    """主源异常连续 3 次自动切换 gate-cli 兜底；返回替换后的 K 线或 None"""
    key = _key(symbol, interval)
    with _lock:
        state = _backup_state.setdefault(key, {"fail_count": 0, "active": False})
        if _is_valid_candle(candle):
            if state["active"] or state["fail_count"]:
                state["fail_count"] = 0
                state["active"] = False
            return None
        state["fail_count"] += 1
        if state["fail_count"] < FAIL_THRESHOLD:
            return None
        state["active"] = True
    t = candle.get("t")
    if t is None:
        return None
    return _fetch_candle_from_cli(symbol, interval, t, market_type)


def is_backup_active():
    """是否有任一品种处于备用源激活状态"""
    with _lock:
        return any(s["active"] for s in _backup_state.values())


def restore_primary_source(symbol=None, interval=None):
    """恢复主数据源（重置失败计数与激活状态）"""
    with _lock:
        if symbol and interval:
            key = _key(symbol, interval)
            if key in _backup_state:
                _backup_state[key] = {"fail_count": 0, "active": False}
        else:
            for k in _backup_state:
                _backup_state[k] = {"fail_count": 0, "active": False}


def get_backup_status():
    """返回各品种备用源状态：{key: active}"""
    with _lock:
        return {k: v["active"] for k, v in _backup_state.items()}
