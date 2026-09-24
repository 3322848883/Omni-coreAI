"""aux_monitor.py — 辅助信息流状态解析与一致性检查（供 status.py / kline_watcher.py 共用）

读取 fetch_aux.py 写入的 aux_status.json，解析出：
  - 采集新鲜度（ts 距今，间隔 60s，允许漏几轮）
  - 各接口状态（interfaces）
  - 滚动缓存表原始状态（db_tables，fetch_aux 采集后汇总）
  - 一致性判定：接口 ok ⟺ 对应表有数据且最新行与采集时间吻合、schema 版本匹配

零脚本原则：本模块只做「读取 + 机械比对」，不下任何分析/交易结论。
"""

import json
import os
import time

import fetch_aux

AUX_FRESH_SEC = 60  # fetch_aux 循环间隔 5s，允许漏几轮（网络抖动）
SCHEMA_VERSION = fetch_aux.SCHEMA_VERSION
# 接口 ok 时，对应时序表最新行与采集时间允许的最大偏差（秒）
TS_GAP_TOLERANCE = 300


def read_aux_status(status_path):
    """读取并解析 aux_status.json；文件缺失/损坏返回 present=False 结构"""
    if not os.path.exists(status_path):
        return {"present": False, "note": "aux_status.json 不存在"}
    try:
        with open(status_path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return {"present": False, "note": "aux_status.json 读取失败: %s" % e}
    ts = data.get("ts")
    if not isinstance(ts, (int, float)):
        return {"present": False, "note": "aux_status.json 缺少 ts"}
    ts_int = int(ts)
    age = int(time.time()) - ts_int
    interfaces = data.get("interfaces") or {}
    failed = [name for name, info in interfaces.items()
              if isinstance(info, dict) and info.get("status") != "ok"]
    db_tables = data.get("db_tables") or {}
    tables = db_tables.get("tables") or {}
    schema_version = db_tables.get("schema_version")

    inconsistencies = []

    def _ts_gap(t):
        lt = tables.get(t, {}).get("latest_ts")
        return None if lt is None else abs(int(lt) - ts_int)

    if interfaces.get("overview", {}).get("status") == "ok":
        if tables.get("overview_ts", {}).get("count", 0) == 0:
            inconsistencies.append("overview 接口 ok 但 overview_ts 无数据")
        elif _ts_gap("overview_ts") is not None and _ts_gap("overview_ts") > TS_GAP_TOLERANCE:
            inconsistencies.append("overview_ts 最新行与采集时间偏差过大")
    if interfaces.get("macro", {}).get("status") == "ok":
        if tables.get("macro_ts", {}).get("count", 0) == 0:
            inconsistencies.append("macro 接口 ok 但 macro_ts 无数据")
        elif _ts_gap("macro_ts") is not None and _ts_gap("macro_ts") > TS_GAP_TOLERANCE:
            inconsistencies.append("macro_ts 最新行与采集时间偏差过大")
    if interfaces.get("events", {}).get("status") == "ok":
        if tables.get("events", {}).get("count", 0) == 0:
            inconsistencies.append("events 接口 ok 但 events 表无数据")
    if interfaces.get("stats", {}).get("status") == "ok":
        if tables.get("market_stats_ts", {}).get("count", 0) == 0:
            inconsistencies.append("stats 接口 ok 但 market_stats_ts 无数据")
        elif _ts_gap("market_stats_ts") is not None and _ts_gap("market_stats_ts") > TS_GAP_TOLERANCE:
            inconsistencies.append("market_stats_ts 最新行与采集时间偏差过大")
    # liquidations 为事件型表：接口成功采集即健康，不做 latest_ts 偏差校验——
    # latest_ts 反映最后一条爆仓数据的采集时间而非本轮采集时间，爆仓不频繁时
    # 偏差必然超容差导致误报；采集停滞由全局 ts 新鲜度（AUX_FRESH_SEC）兜底
    if interfaces.get("orderbook", {}).get("status") == "ok":
        if tables.get("orderbook_snap", {}).get("count", 0) == 0:
            inconsistencies.append("orderbook 接口 ok 但 orderbook_snap 无数据")
        elif _ts_gap("orderbook_snap") is not None and _ts_gap("orderbook_snap") > TS_GAP_TOLERANCE:
            inconsistencies.append("orderbook_snap 最新行与采集时间偏差过大")
    if interfaces.get("trades", {}).get("status") == "ok":
        if tables.get("trades", {}).get("count", 0) == 0:
            inconsistencies.append("trades 接口 ok 但 trades 表无数据")
        elif _ts_gap("trades") is not None and _ts_gap("trades") > TS_GAP_TOLERANCE:
            inconsistencies.append("trades 最新行与采集时间偏差过大")
    if interfaces.get("rankings", {}).get("status") == "ok":
        if tables.get("coin_rankings_ts", {}).get("count", 0) == 0:
            inconsistencies.append("rankings 接口 ok 但 coin_rankings_ts 无数据")
        elif _ts_gap("coin_rankings_ts") is not None and _ts_gap("coin_rankings_ts") > TS_GAP_TOLERANCE:
            inconsistencies.append("coin_rankings_ts 最新行与采集时间偏差过大")
    # announcements 为事件型表：接口成功采集即健康（无新公告时段 fetched_ts 不更新，
    # 偏差校验会误报）；采集停滞由全局 ts 新鲜度兜底
    if interfaces.get("social", {}).get("status") == "ok":
        if tables.get("social_posts_ts", {}).get("count", 0) == 0:
            inconsistencies.append("social 接口 ok 但 social_posts_ts 无数据")
        elif _ts_gap("social_posts_ts") is not None and _ts_gap("social_posts_ts") > TS_GAP_TOLERANCE:
            inconsistencies.append("social_posts_ts 最新行与采集时间偏差过大")
    if interfaces.get("coin_info", {}).get("status") == "ok":
        if tables.get("coin_info_ts", {}).get("count", 0) == 0:
            inconsistencies.append("coin_info 接口 ok 但 coin_info_ts 无数据")
        elif _ts_gap("coin_info_ts") is not None and _ts_gap("coin_info_ts") > TS_GAP_TOLERANCE:
            inconsistencies.append("coin_info_ts 最新行与采集时间偏差过大")
    if interfaces.get("tech_analysis", {}).get("status") == "ok":
        if tables.get("tech_analysis_ts", {}).get("count", 0) == 0:
            inconsistencies.append("tech_analysis 接口 ok 但 tech_analysis_ts 无数据")
        elif _ts_gap("tech_analysis_ts") is not None and _ts_gap("tech_analysis_ts") > TS_GAP_TOLERANCE:
            inconsistencies.append("tech_analysis_ts 最新行与采集时间偏差过大")
    # onchain 为时序表：每轮必然写入（ETH 链上数据恒定存在），可做 latest_ts 偏差校验
    if interfaces.get("onchain", {}).get("status") == "ok":
        if tables.get("onchain_ts", {}).get("count", 0) == 0:
            inconsistencies.append("onchain 接口 ok 但 onchain_ts 无数据")
        elif _ts_gap("onchain_ts") is not None and _ts_gap("onchain_ts") > TS_GAP_TOLERANCE:
            inconsistencies.append("onchain_ts 最新行与采集时间偏差过大")
    # event_signals 为事件型表：无新事件时段 fetched_ts 不更新（INSERT OR REPLACE
    # 仅在事件集合非空时写入），偏差校验会误报；采集停滞由全局 ts 新鲜度兜底
    if interfaces.get("event_signals", {}).get("status") == "ok":
        if tables.get("event_signals", {}).get("count", 0) == 0:
            inconsistencies.append("event_signals 接口 ok 但 event_signals 表无数据")
    if schema_version != SCHEMA_VERSION:
        inconsistencies.append("schema_version 异常: %s" % schema_version)

    return {
        "present": True,
        "last_fetch_ts": ts_int,
        "last_fetch_at": data.get("fetched_at"),
        "age_sec": age,
        "fresh": age <= AUX_FRESH_SEC,
        "interfaces": interfaces,
        "failed_interfaces": failed,
        "db_tables": db_tables,
        "consistent": len(inconsistencies) == 0,
        "inconsistencies": inconsistencies,
    }
