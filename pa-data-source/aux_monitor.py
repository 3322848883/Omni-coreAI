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


# aux 各表用来对币的列（必须与 `omnialpha/strategist/tools.py` 的查询一致，
# 否则统计的是「别的东西有没有数据」而不是这个币）。
SYMBOL_TABLES = (
    ("trades", "contract"),
    ("liquidations", "contract"),
    ("market_stats_ts", "contract"),
    ("coin_info_ts", "symbol"),
    ("onchain_ts", "token"),
    ("social_posts_ts", "coin"),
    ("sentiment_ts", "coin"),
)


def symbol_coverage(db_path, contracts):
    """每个币在 aux 各表里的行数 → `{symbol: {table: rows}}`；读不到返回 `{}`。

    **为什么必须按币**：`interfaces[...].status` 只给**整体** ok/fail —— 5 个币里
    1 个全空也照样报 ok（只要别的币有数据）。而「新增/切换的币永远没数据」正是这种
    形态：aux 工具静默返回空列表，模型读成「这个币没数据」（审计 A-7/A-8，
    静默空比报错更难发现）。

    零脚本原则：只数行数，不下任何分析结论。
    """
    if not db_path or not os.path.exists(db_path):
        return {}
    import sqlite3

    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            have = {r[0] for r in con.execute(
                "select name from sqlite_master where type='table'")}
            for sym in (contracts or []):
                key = str(sym or "").strip().upper()
                if not key:
                    continue
                per = {}
                for table, col in SYMBOL_TABLES:
                    if table not in have:
                        continue
                    try:
                        n = con.execute(
                            "select count(*) from %s where upper(%s)=?" % (table, col),
                            (key,)).fetchone()[0]
                    except sqlite3.Error:
                        continue  # 该表缺列/结构不同 → 跳过，不当成「零数据」
                    per[table] = int(n or 0)
                if per:
                    out[key] = per
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — 只读统计，失败就当没有这一层
        return out
    return out


def zero_coverage_symbols(coverage):
    """**所有表都零行**的币 —— 这些币的 aux 数据等于不存在（要么去采、要么告警）。"""
    return sorted(sym for sym, per in (coverage or {}).items()
                  if per and all(int(v or 0) == 0 for v in per.values()))


def read_aux_status(status_path, db_path=None, contracts=None):
    """读取并解析 aux_status.json；文件缺失/损坏返回 present=False 结构

    可选 `db_path` + `contracts`：给了就附上**按币覆盖**（`symbol_coverage` /
    `zero_coverage`），用于回答「这个币的 aux 到底有没有数据」。
    两个都不给时行为与改动前完全一致。
    """
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

    # 按币覆盖（可选层）：整体 ok 只说明「有数据」，说不出**哪个币**没数据 ——
    # 而「新增/切换的币永远没数据」正是整体 ok 掩盖的那种形态（A-7）。
    coverage = symbol_coverage(db_path, contracts) if db_path else {}
    zero = zero_coverage_symbols(coverage)
    for sym in zero:
        inconsistencies.append("%s 在 aux 各表全为零行（这个币的 aux 数据等于不存在）" % sym)

    out = {
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
    if db_path:
        out["symbol_coverage"] = coverage
        out["zero_coverage"] = zero
    return out
