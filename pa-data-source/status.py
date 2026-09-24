#!/usr/bin/env python3
"""status.py — 运行状态一键查询（供 AI/人工快速判断系统是否在跑）

用法:
    python status.py            # 人读格式（默认）
    python status.py --json     # JSON 格式（AI 用）

检查项:
    1. watchdog / kline_watcher / fetch_aux 进程存活（读各自锁文件 PID）
    2. 健康检查端点 http://127.0.0.1:18080/health 应答
    3. 数据新鲜度汇总（从健康端点取，30 路数据源多少新鲜/停滞）
    4. 辅助信息流新鲜度（读 aux-data/aux_status.json，fetch_aux 循环间隔 5s，允许漏几轮）
    5. 最近日志错误（最近 2h 内 ERROR/WARNING 条数，K线 + 辅助信息流）

退出码: 0=系统健康运行中, 1=部分异常, 2=未运行
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta

from aux_monitor import read_aux_status

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHDOG_LOCK = os.path.join(SCRIPT_DIR, "watchdog.lock")
KLINE_LOCK = os.path.join(SCRIPT_DIR, "kline_watcher.lock")
AUX_DIR = os.path.join(SCRIPT_DIR, "aux-data")
AUX_LOCK = os.path.join(AUX_DIR, "aux_fetch.lock")
AUX_STATUS = os.path.join(AUX_DIR, "aux_status.json")
HEALTH_URL = "http://127.0.0.1:18080/health"
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
KLINE_LOG = os.path.join(LOG_DIR, "kline_watcher.log")
AUX_LOG = os.path.join(AUX_DIR, "logs", "aux_info_feed.log")


def _pid_alive(pid):
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _read_lock_pid(path):
    try:
        with open(path, encoding="utf-8") as f:
            pid = int(f.read().strip())
            return pid if pid > 0 else None
    except Exception:
        return None


def check_processes():
    """读锁文件判断 watchdog / kline_watcher / fetch_aux 是否存活"""
    result = {}
    for name, lock in (("watchdog", WATCHDOG_LOCK),
                       ("kline_watcher", KLINE_LOCK),
                       ("fetch_aux", AUX_LOCK)):
        pid = _read_lock_pid(lock)
        if pid is None:
            result[name] = {"running": False, "pid": None,
                            "note": "无锁文件（未以带锁方式启动或已退出）"}
        else:
            alive = _pid_alive(pid)
            result[name] = {"running": alive, "pid": pid,
                            "note": None if alive else "锁文件存在但 PID 已死（孤儿锁）"}
    return result


def check_aux_status():
    """读 aux_status.json，返回辅助信息流新鲜度、滚动缓存表状态与一致性"""
    return read_aux_status(AUX_STATUS)


def check_health_endpoint():
    """请求健康端点，返回 (ok, data/err)"""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, str(e)


def summarize_freshness(health_data):
    """从健康数据提取数据源新鲜度汇总"""
    if not isinstance(health_data, dict):
        return None
    freshness = health_data.get("data_freshness")
    if not isinstance(freshness, dict):
        return None
    total = freshness.get("total")
    stale = freshness.get("stale")
    if not isinstance(total, int) or not isinstance(stale, int):
        return None
    return {"sources": total, "fresh": total - stale, "stale": stale,
            "oldest_age_sec": None}


def check_recent_log_errors(log_path=KLINE_LOG, hours=2):
    """统计最近 N 小时日志中的 ERROR / WARNING"""
    if not os.path.exists(log_path):
        return {"available": False, "note": "日志文件不存在"}
    cutoff = datetime.now() - timedelta(hours=hours)
    pat = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[(ERROR|WARNING)\]")
    counts = {"ERROR": 0, "WARNING": 0}
    try:
        # 只读文件尾部 512KB，避免大日志全读
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            if size > 512 * 1024:
                f.seek(size - 512 * 1024)
                f.readline()  # 跳过半行
            chunk = f.read().decode("utf-8", errors="replace")
        for m in pat.finditer(chunk):
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            if ts >= cutoff:
                counts[m.group(2)] += 1
        return {"available": True, "window_hours": hours, **counts}
    except Exception as e:
        return {"available": False, "note": str(e)}


def build_report():
    procs = check_processes()
    health_ok, health_data = check_health_endpoint()
    fresh = summarize_freshness(health_data) if health_ok else None
    logs = check_recent_log_errors(KLINE_LOG)
    aux = check_aux_status()
    aux_logs = check_recent_log_errors(AUX_LOG)

    kline_running = procs["kline_watcher"]["running"] or health_ok
    aux_running = procs.get("fetch_aux", {}).get("running", False)
    aux_interfaces = aux.get("interfaces") or {}
    aux_all_failed = bool(aux_interfaces) and all(
        isinstance(i, dict) and i.get("status") != "ok" for i in aux_interfaces.values())
    aux_healthy = (aux_running and aux.get("present") and aux.get("fresh")
                   and not aux_all_failed and aux.get("consistent", True))
    overall = "stopped" if not kline_running else (
        "healthy" if (health_ok and (not fresh or fresh["stale"] == 0)
                      and logs.get("ERROR", 99) == 0
                      and aux_healthy) else "degraded")
    if kline_running and not health_ok:
        overall = "degraded"

    return {
        "overall": overall,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "processes": procs,
        "health_endpoint": {
            "url": HEALTH_URL, "responding": health_ok,
            "status_code": (health_data or {}).get("status") if health_ok else None,
            "error": None if health_ok else health_data,
        },
        "data_freshness": fresh,
        "aux_info": aux,
        "aux_logs": aux_logs,
        "recent_logs": logs,
        "hint": {
            "stopped": "系统未运行。启动: python watchdog.py（守护模式）或双击 launcher.vbs",
            "degraded": "部分异常，详见各字段。可查 logs/kline_watcher.log 与 aux-data/logs/aux_info_feed.log 定位",
            "healthy": "运行正常。健康详情: " + HEALTH_URL,
        }[overall],
    }


def main():
    ap = argparse.ArgumentParser(description="pa-data-source 运行状态查询")
    ap.add_argument("--json", action="store_true", help="输出 JSON（AI 用）")
    args = ap.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0 if report["overall"] == "healthy" else
                 2 if report["overall"] == "stopped" else 1)

    p = report["processes"]
    h = report["health_endpoint"]
    f = report["data_freshness"]
    lg = report["recent_logs"]
    aux = report["aux_info"]
    aux_lg = report["aux_logs"]
    print(f"=== pa-data-source 状态 @ {report['timestamp']} ===")
    print(f"总评: {report['overall']}")
    print()
    print(f"watchdog:      {'运行中 PID ' + str(p['watchdog']['pid']) if p['watchdog']['running'] else '未运行'}"
          + (f"  ({p['watchdog']['note']})" if p['watchdog']['note'] else ""))
    print(f"kline_watcher: {'运行中 PID ' + str(p['kline_watcher']['pid']) if p['kline_watcher']['running'] else '未运行'}"
          + (f"  ({p['kline_watcher']['note']})" if p['kline_watcher']['note'] else ""))
    print(f"fetch_aux:     {'运行中 PID ' + str(p['fetch_aux']['pid']) if p['fetch_aux']['running'] else '未运行'}"
          + (f"  ({p['fetch_aux']['note']})" if p['fetch_aux']['note'] else ""))
    print(f"健康端点:      {'正常' if h['responding'] else '无响应 - ' + str(h['error'])[:60]}")
    if f:
        print(f"数据新鲜度:    {f['fresh']}/{f['sources']} 路新鲜, {f['stale']} 路停滞"
              + (f" (最旧 {f['oldest_age_sec']}s 前)" if f["oldest_age_sec"] is not None else ""))
    if aux.get("present"):
        aux_state = "新鲜" if aux["fresh"] else "停滞"
        print(f"辅助信息流:    {aux_state} (上次采集 {aux['age_sec']}s 前, {aux['last_fetch_at']})"
              + (f"  失败接口: {','.join(aux['failed_interfaces'])}" if aux["failed_interfaces"] else ""))
        if aux.get("consistent"):
            print(f"缓存表一致性:  一致 (schema v{aux.get('db_tables', {}).get('schema_version')})")
        else:
            print(f"缓存表一致性:  不一致 -> {'; '.join(aux.get('inconsistencies', []))}")
        tables = (aux.get("db_tables") or {}).get("tables") or {}
        if tables:
            parts = []
            for t in ("events", "overview_ts", "macro_ts", "macro_events"):
                info = tables.get(t) or {}
                parts.append(f"{t}={info.get('count', '?')}条")
            print(f"滚动缓存表:    {', '.join(parts)}")
    else:
        print(f"辅助信息流:    {aux.get('note', '未初始化')}")
    if lg.get("available"):
        print(f"近{lg['window_hours']}h日志:    ERROR {lg['ERROR']} 条 / WARNING {lg['WARNING']} 条")
    if aux_lg.get("available"):
        print(f"辅助流近{aux_lg['window_hours']}h日志: ERROR {aux_lg['ERROR']} 条 / WARNING {aux_lg['WARNING']} 条")
    print()
    print(f"→ {report['hint']}")
    sys.exit(0 if report["overall"] == "healthy" else
             2 if report["overall"] == "stopped" else 1)


if __name__ == "__main__":
    main()
