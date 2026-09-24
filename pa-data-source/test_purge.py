import json
import os
import sqlite3
import sys
import tempfile
import time

import fetch_aux as fa

HOUR = 3600
DAY = 86400
NOW = int(time.time())
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, detail))


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, event_type TEXT, event_time TEXT,
        event_title TEXT, impact_direction TEXT, impact_direction_reason TEXT,
        impact_analysis TEXT, price_change TEXT, context TEXT,
        related_coins TEXT, tags TEXT, fetched_ts INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS overview_ts (
        fetched_ts INTEGER PRIMARY KEY, btc_price REAL, btc_dominance REAL,
        eth_dominance REAL, fear_greed INTEGER, fear_greed_label TEXT,
        total_market_cap REAL, total_volume_24h REAL, market_cap_change_24h REAL,
        altcoin_season_index REAL, ahr999 REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS macro_ts (
        fetched_ts INTEGER PRIMARY KEY, cpi_yoy REAL, fed_funds_rate REAL,
        gdp_growth REAL, nonfarm_payroll REAL, pce_yoy REAL, unemployment_rate REAL,
        raw TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS macro_events (
        event_id TEXT PRIMARY KEY, event_date TEXT, event_name TEXT,
        event_type TEXT, event_time TEXT, status TEXT, fetched_ts INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sentiment_ts (
        fetched_ts INTEGER PRIMARY KEY, coin TEXT, mention_count INTEGER,
        overall_sentiment REAL, sentiment_label TEXT, positive_ratio REAL,
        neutral_ratio REAL, negative_ratio REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reserves_ts (
        fetched_ts INTEGER PRIMARY KEY, asset TEXT, reserve_amount REAL,
        reserve_usd REAL, change_24h_pct REAL, change_7d_pct REAL,
        change_30d_pct REAL, snapshot_time TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS market_stats_ts (
        fetched_ts INTEGER, contract TEXT,
        lsr_taker REAL, lsr_account REAL,
        long_liq_size REAL, short_liq_size REAL,
        open_interest REAL, open_interest_usd REAL,
        top_lsr_account REAL, top_lsr_size REAL,
        mark_price REAL,
        PRIMARY KEY (fetched_ts, contract)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS liquidations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time INTEGER, contract TEXT,
        size REAL, order_size REAL, order_price REAL, fill_price REAL,
        fetched_ts INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orderbook_snap (
        fetched_ts INTEGER, contract TEXT,
        bids TEXT, asks TEXT,
        PRIMARY KEY (fetched_ts, contract)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        trade_id INTEGER, time INTEGER, contract TEXT,
        price REAL, size REAL, fetched_ts INTEGER,
        PRIMARY KEY (trade_id, contract)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS coin_rankings_ts (
        fetched_ts INTEGER, ranking_type TEXT, symbol TEXT, name TEXT,
        latest_price REAL, price_change_24h REAL, market_cap REAL, rank INTEGER,
        PRIMARY KEY (fetched_ts, ranking_type, symbol)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS exchange_announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notice_id TEXT, title TEXT, content TEXT, platform TEXT,
        notice_type TEXT, classify_name TEXT, publish_time TEXT, url TEXT,
        fetched_ts INTEGER, UNIQUE (notice_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS social_posts_ts (
        fetched_ts INTEGER, coin TEXT, post_id TEXT, author TEXT, channel TEXT,
        platform TEXT, content TEXT, upvotes INTEGER, comment_count INTEGER,
        sentiment_label TEXT, sentiment_score REAL, quality_tier TEXT,
        created_time TEXT, url TEXT, PRIMARY KEY (post_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS coin_info_ts (
        fetched_ts INTEGER, symbol TEXT, name TEXT, name_cn TEXT,
        chain TEXT, contract_address TEXT, category TEXT, exchange_type TEXT,
        market_value REAL, sentiment_score REAL, part_date TEXT,
        PRIMARY KEY (fetched_ts, symbol)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tech_analysis_ts (
        fetched_ts INTEGER, symbol TEXT, period TEXT, signal TEXT,
        timeframes_json TEXT, PRIMARY KEY (fetched_ts, symbol)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS onchain_ts (
        fetched_ts INTEGER, token TEXT, chain TEXT,
        daily_active_addresses INTEGER, daily_transfer_volume REAL,
        new_address_count_7d INTEGER, holder_count INTEGER, data_quality TEXT,
        PRIMARY KEY (fetched_ts, token)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS event_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_ref TEXT, event_name TEXT, window TEXT,
        outcome_probabilities TEXT, directional_context TEXT, volume_flow TEXT,
        fetched_ts INTEGER, UNIQUE (event_ref, window)
    )""")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '9')"
    )
    conn.commit()
    return conn


def test_event_tiered_purge():
    print("== 事件按级别分档清理 ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    cases = [
        # (event_id, event_type, age_sec, expect_survive)
        ("maj_old", "major_even", 25 * HOUR, False),   # >24h 删
        ("maj_new", "major_even", 1 * HOUR, True),      # <24h 留
        ("mkt_old", "market_move", 13 * HOUR, False),   # >12h 删
        ("mkt_new", "market_move", 2 * HOUR, True),     # <12h 留
        ("coin_old", "coin_move", 7 * HOUR, False),     # >6h 删
        ("coin_new", "coin_move", 30 * 60, True),       # <6h 留
        ("unk_old", "weird_type", 7 * HOUR, False),     # 未知级别兜底6h 删
        ("unk_new", "weird_type", 1 * HOUR, True),      # 未知级别 <6h 留
    ]
    for eid, etype, age, _ in cases:
        conn.execute(
            "INSERT INTO events (event_id, event_type, event_title, fetched_ts) "
            "VALUES (?,?,?,?)",
            (eid, etype, eid, NOW - age),
        )
    conn.commit()
    deleted = fa.purge_old(conn)
    check("major_even 删 1", deleted.get("events:major_even") == 1, deleted)
    check("market_move 删 1", deleted.get("events:market_move") == 1, deleted)
    check("coin_move 删 1", deleted.get("events:coin_move") == 1, deleted)
    check("未知级别 删 1", deleted.get("events:other") == 1, deleted)
    for eid, _, _, expect in cases:
        row = conn.execute(
            "SELECT 1 FROM events WHERE event_id=?", (eid,)
        ).fetchone()
        check("事件 %s %s" % (eid, "保留" if expect else "清除"),
              (row is not None) == expect)
    conn.close()
    os.remove(path)


def test_ts_ttl():
    print("== 时序表 TTL 清理 ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    ts_cases = [
        ("overview_ts", 31 * DAY, 1 * DAY),
        ("macro_ts", 91 * DAY, 1 * DAY),
        ("sentiment_ts", 15 * DAY, 1 * DAY),
        ("reserves_ts", 91 * DAY, 1 * DAY),
        ("market_stats_ts", 91 * DAY, 1 * DAY),
        ("liquidations", 25 * HOUR, 1 * HOUR),
        ("orderbook_snap", 2 * HOUR, 30 * 60),
        ("trades", 25 * HOUR, 1 * HOUR),
        ("coin_rankings_ts", 8 * DAY, 1 * DAY),
        ("exchange_announcements", 8 * DAY, 1 * DAY),
        ("social_posts_ts", 4 * DAY, 1 * DAY),
        ("coin_info_ts", 31 * DAY, 1 * DAY),
        ("tech_analysis_ts", 8 * DAY, 1 * DAY),
        ("onchain_ts", 31 * DAY, 1 * DAY),
        ("event_signals", 8 * DAY, 1 * DAY),
    ]
    for table, old_age, new_age in ts_cases:
        conn.execute(
            "INSERT INTO %s (fetched_ts) VALUES (?)" % table, (NOW - old_age,)
        )
        conn.execute(
            "INSERT INTO %s (fetched_ts) VALUES (?)" % table, (NOW - new_age,)
        )
    conn.commit()
    deleted = fa.purge_old(conn)
    for table, _, _ in ts_cases:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM %s" % table
        ).fetchone()[0]
        check("%s 剩 1 条(旧删新留)" % table, remaining == 1,
              "remaining=%s deleted=%s" % (remaining, deleted.get(table)))
        check("%s 删 1 条" % table, deleted.get(table) == 1, deleted)
    conn.close()
    os.remove(path)


def test_boundary_exact_ttl():
    print("== 边界：恰好等于 TTL 保留（fetched_ts < now-ttl 才删）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (event_id, event_type, fetched_ts) VALUES (?,?,?)",
        ("maj_exact", "major_even", now - 24 * HOUR),
    )
    conn.execute(
        "INSERT INTO overview_ts (fetched_ts) VALUES (?)", (now - 30 * DAY,)
    )
    conn.commit()
    fa.purge_old(conn)
    check("事件恰好24h 保留", conn.execute(
        "SELECT 1 FROM events WHERE event_id='maj_exact'").fetchone() is not None)
    check("overview 恰好30d 保留", conn.execute(
        "SELECT COUNT(*) FROM overview_ts").fetchone()[0] == 1)
    conn.close()
    os.remove(path)


def test_schema_migration():
    print("== schema 版本迁移：旧 v1 库自动重建为最新版 ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version','1')")
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, fetched_ts INTEGER)")
    conn.execute("INSERT INTO events VALUES ('legacy', 1)")
    conn.commit()
    conn.close()
    conn = fa._init_db(path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("重建后含 17 张缓存表", all(
        t in tables for t in ("events", "overview_ts", "macro_ts",
                              "sentiment_ts", "reserves_ts", "macro_events",
                              "market_stats_ts", "liquidations", "orderbook_snap", "trades",
                              "coin_rankings_ts", "exchange_announcements",
                              "social_posts_ts", "coin_info_ts", "tech_analysis_ts",
                              "onchain_ts", "event_signals")), tables)
    check("旧数据已清", conn.execute(
        "SELECT COUNT(*) FROM events").fetchone()[0] == 0)
    check("meta 版本=9", conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "9")
    conn.close()
    os.remove(path)


def test_schema_v8_to_v9_migration():
    print("== schema v8→v9 定向迁移：trades 复合主键 + 数据保留 ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version','8')")
    conn.execute("""CREATE TABLE trades (
        trade_id INTEGER PRIMARY KEY,
        time INTEGER, contract TEXT,
        price REAL, size REAL, fetched_ts INTEGER
    )""")
    conn.execute("INSERT INTO trades VALUES (100, 1, 'BTC_USDT', 78000, 1, 1)")
    conn.execute("INSERT INTO trades VALUES (101, 2, 'ETH_USDT', 2400, 2, 1)")
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, fetched_ts INTEGER)")
    conn.execute("INSERT INTO events VALUES ('e1', 1)")
    conn.commit()
    conn.close()
    conn = fa._init_db(path)
    check("meta 版本=9", conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "9")
    check("trades 行数保留", conn.execute(
        "SELECT COUNT(*) FROM trades").fetchone()[0] == 2)
    check("非 trades 表数据不动", conn.execute(
        "SELECT COUNT(*) FROM events").fetchone()[0] == 1)
    check("trades 无残留旧表", conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='trades_old_v8'"
    ).fetchone()[0] == 0)
    pk = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='trades'").fetchone()[0]
    check("复合主键 (trade_id, contract)",
          "PRIMARY KEY (trade_id, contract)" in pk, pk)
    idx = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
        "AND tbl_name='trades' AND name='idx_trades_time'").fetchone()[0]
    check("time 索引重建", idx == 1)
    conn.close()
    os.remove(path)


def test_macro_events_purge():
    print("== 宏观日历按 event_date 清理（过期删、未到留）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    today = time.strftime("%Y-%m-%d", time.localtime())
    conn.execute(
        "INSERT INTO macro_events (event_id, event_date, event_name) VALUES (?,?,?)",
        ("past_evt", "2000-01-01", "已过期事件"),
    )
    conn.execute(
        "INSERT INTO macro_events (event_id, event_date, event_name) VALUES (?,?,?)",
        ("today_evt", today, "今天事件"),
    )
    conn.execute(
        "INSERT INTO macro_events (event_id, event_date, event_name) VALUES (?,?,?)",
        ("future_evt", "2099-12-31", "未来事件"),
    )
    conn.commit()
    deleted = fa.purge_old(conn)
    check("过期事件删 1", deleted.get("macro_events") == 1, deleted)
    check("今天事件保留", conn.execute(
        "SELECT 1 FROM macro_events WHERE event_id='today_evt'").fetchone() is not None)
    check("未来事件保留", conn.execute(
        "SELECT 1 FROM macro_events WHERE event_id='future_evt'").fetchone() is not None)
    check("过期事件清除", conn.execute(
        "SELECT 1 FROM macro_events WHERE event_id='past_evt'").fetchone() is None)
    conn.close()
    os.remove(path)


def test_unknown_level_fallback():
    print("== 未知级别兜底 6h ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    conn.execute(
        "INSERT INTO events (event_id, event_type, fetched_ts) VALUES (?,?,?)",
        ("unk_5h", "mystery", NOW - 5 * HOUR),
    )
    conn.execute(
        "INSERT INTO events (event_id, event_type, fetched_ts) VALUES (?,?,?)",
        ("unk_7h", "mystery", NOW - 7 * HOUR),
    )
    conn.commit()
    fa.purge_old(conn)
    check("未知级别 5h 保留", conn.execute(
        "SELECT 1 FROM events WHERE event_id='unk_5h'").fetchone() is not None)
    check("未知级别 7h 清除", conn.execute(
        "SELECT 1 FROM events WHERE event_id='unk_7h'").fetchone() is None)
    conn.close()
    os.remove(path)


def test_collect_db_tables():
    print("== 滚动缓存表状态采集（db_tables）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (event_id, event_type, fetched_ts) VALUES (?,?,?)",
        ("e1", "major_even", now - 100),
    )
    conn.execute(
        "INSERT INTO events (event_id, event_type, fetched_ts) VALUES (?,?,?)",
        ("e2", "coin_move", now - 50),
    )
    conn.execute("INSERT INTO overview_ts (fetched_ts) VALUES (?)", (now - 30,))
    conn.execute("INSERT INTO overview_ts (fetched_ts) VALUES (?)", (now - 10,))
    conn.execute("INSERT INTO macro_ts (fetched_ts) VALUES (?)", (now - 20,))
    conn.execute(
        "INSERT INTO macro_events (event_id, event_date) VALUES (?,?)",
        ("m1", "2099-12-31"),
    )
    conn.commit()
    info = fa._collect_db_tables(conn)
    tables = info["tables"]
    check("schema_version=9", info.get("schema_version") == 9, info)
    check("events 计数 2", tables["events"]["count"] == 2, tables["events"])
    check("events 最新时间", tables["events"]["latest_ts"] == now - 50, tables["events"])
    check("overview_ts 计数 2", tables["overview_ts"]["count"] == 2, tables["overview_ts"])
    check("overview_ts 最新时间", tables["overview_ts"]["latest_ts"] == now - 10, tables["overview_ts"])
    check("macro_ts 最新时间", tables["macro_ts"]["latest_ts"] == now - 20, tables["macro_ts"])
    check("macro_events 最新日期", tables["macro_events"]["latest_date"] == "2099-12-31", tables["macro_events"])
    check("sentiment_ts 空表计数 0", tables["sentiment_ts"]["count"] == 0, tables["sentiment_ts"])
    conn.close()
    os.remove(path)


def test_store_sentiment():
    print("== sentiment 落库（sentiment_ts）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    fa._store_sentiment(conn, {
        "coin": "BTC",
        "mention_count": 7,
        "overall_sentiment": 0.3775,
        "sentiment_label": "positive",
        "sentiment_distribution": {
            "positive_ratio": 0.714286,
            "neutral_ratio": 0.142857,
            "negative_ratio": 0.142857,
        },
    })
    row = conn.execute("SELECT * FROM sentiment_ts").fetchone()
    check("sentiment 落库 1 行", row is not None)
    if row:
        check("coin=BTC", row[1] == "BTC", row)
        check("mention_count=7", row[2] == 7, row)
        check("overall_sentiment=0.3775", abs(row[3] - 0.3775) < 1e-6, row)
        check("label=positive", row[4] == "positive", row)
        check("positive_ratio", abs(row[5] - 0.714286) < 1e-6, row)
    conn.close()
    os.remove(path)


def test_store_reserves():
    print("== reserves 落库 + 同快照去重（reserves_ts）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = {
        "items": [{
            "asset": "BTC",
            "reserve_amount": 2724451.2,
            "reserve_usd": 211757711000,
            "change_24h_pct": 0.84,
            "change_7d_pct": 23.13,
            "change_30d_pct": 22.07,
            "snapshot_time": "2026-08-24T02:09:56.000+0000",
        }]
    }
    fa._store_reserves(conn, data)
    check("reserves 首次落库 1 行", conn.execute(
        "SELECT COUNT(*) FROM reserves_ts").fetchone()[0] == 1)
    fa._store_reserves(conn, data)  # 同一快照，应跳过
    check("同快照去重仍 1 行", conn.execute(
        "SELECT COUNT(*) FROM reserves_ts").fetchone()[0] == 1)
    time.sleep(1.1)  # 避开 fetched_ts 主键同秒冲突（生产每日采集不会同秒）
    data["items"][0]["snapshot_time"] = "2026-08-25T02:09:56.000+0000"
    fa._store_reserves(conn, data)  # 新快照，应新增
    check("新快照新增 1 行", conn.execute(
        "SELECT COUNT(*) FROM reserves_ts").fetchone()[0] == 2)
    row = conn.execute(
        "SELECT asset, reserve_amount, snapshot_time FROM reserves_ts "
        "ORDER BY fetched_ts DESC LIMIT 1").fetchone()
    check("reserves 字段正确", row and row[0] == "BTC" and row[2] == "2026-08-25T02:09:56.000+0000", row)
    conn.close()
    os.remove(path)


def test_store_stats():
    print("== stats 落库（market_stats_ts）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    fa._store_stats(conn, [{
        "lsr_taker": 0.725, "lsr_account": 0.91,
        "long_liq_size": "0", "short_liq_size": "0",
        "open_interest": "588483656", "open_interest_usd": 4631589996.5,
        "top_lsr_account": 0.718, "top_lsr_size": "1.549",
        "mark_price": 78703.8,
    }], "BTC_USDT")
    row = conn.execute("SELECT * FROM market_stats_ts").fetchone()
    check("stats 落库 1 行", row is not None)
    if row:
        check("contract=BTC_USDT", row[1] == "BTC_USDT", row)
        check("lsr_taker=0.725", abs(row[2] - 0.725) < 1e-6, row)
        check("open_interest 字符串转 float", abs(row[6] - 588483656) < 1, row)
        check("mark_price=78703.8", abs(row[10] - 78703.8) < 1e-6, row)
    conn.close()
    os.remove(path)


def test_store_liquidations():
    print("== liquidations 落库 + time+contract 去重 ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = [{
        "time": 1787612589, "contract": "BTC_USDT",
        "size": "3", "order_size": "-3",
        "order_price": "78363.8", "fill_price": "78598.3",
    }]
    fa._store_liquidations(conn, data, "BTC_USDT")
    check("liquidations 首次落库 1 行", conn.execute(
        "SELECT COUNT(*) FROM liquidations").fetchone()[0] == 1)
    fa._store_liquidations(conn, data, "BTC_USDT")  # 同 time+contract，跳过
    check("同 time+contract 去重仍 1 行", conn.execute(
        "SELECT COUNT(*) FROM liquidations").fetchone()[0] == 1)
    row = conn.execute("SELECT * FROM liquidations").fetchone()
    check("liquidations 字段正确", row and row[2] == "BTC_USDT"
          and abs(row[3] - 3) < 1e-6 and abs(row[4] + 3) < 1e-6, row)
    conn.close()
    os.remove(path)


def test_store_orderbook():
    print("== orderbook 落库（orderbook_snap）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = {
        "current": 1787614930.933, "update": 1787614930.933,
        "asks": [{"p": "78909.7", "s": "54165"}, {"p": "78909.8", "s": "48"}],
        "bids": [{"p": "78909.6", "s": "21687"}, {"p": "78909.4", "s": "696"}],
    }
    fa._store_orderbook(conn, data, "BTC_USDT")
    row = conn.execute("SELECT * FROM orderbook_snap").fetchone()
    check("orderbook 落库 1 行", row is not None)
    if row:
        check("contract=BTC_USDT", row[1] == "BTC_USDT", row)
        bids = json.loads(row[2])
        check("bids 存 2 档", len(bids) == 2, row[2])
        check("bids 首档价格", bids[0]["p"] == "78909.6", bids)
        asks = json.loads(row[3])
        check("asks 存 2 档", len(asks) == 2, row[3])
    conn.close()
    os.remove(path)


def test_store_trades():
    print("== trades 落库 + (trade_id, contract) 去重 ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = [{
        "id": 817769540, "create_time": 1787614917.355,
        "contract": "BTC_USDT", "size": "7", "price": "78908.2",
    }]
    fa._store_trades(conn, data, "BTC_USDT")
    check("trades 首次落库 1 行", conn.execute(
        "SELECT COUNT(*) FROM trades").fetchone()[0] == 1)
    fa._store_trades(conn, data, "BTC_USDT")  # 同 trade_id，跳过
    check("同 trade_id 去重仍 1 行", conn.execute(
        "SELECT COUNT(*) FROM trades").fetchone()[0] == 1)
    fa._store_trades(conn, [{
        "id": 817769540, "create_time": 1787614917.355,
        "contract": "ETH_USDT", "size": "3", "price": "2400.5",
    }], "ETH_USDT")  # 跨合约同 trade_id：Gate 各合约独立编号，必须保留
    check("跨合约同 trade_id 各自落库", conn.execute(
        "SELECT COUNT(*) FROM trades").fetchone()[0] == 2)
    row = conn.execute(
        "SELECT * FROM trades WHERE contract='BTC_USDT'").fetchone()
    check("trades 字段正确", row and row[0] == 817769540
          and abs(row[3] - 78908.2) < 1e-6 and abs(row[4] - 7) < 1e-6, row)
    conn.close()
    os.remove(path)


def test_store_rankings():
    print("== rankings 落库 + 复合主键去重（coin_rankings_ts）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = {
        "items": [
            {"symbol": "FLT", "name": "FLT", "latest_price": 0.01,
             "price_change_24h": 29.5, "market_cap": None, "rank": 1},
            {"symbol": "MU3S", "name": "MU3S", "latest_price": 0.12,
             "price_change_24h": 18.41, "market_cap": None, "rank": 2},
        ],
        "ranking_type": "top_gainers",
    }
    fa._store_rankings(conn, data, "top_gainers")
    check("rankings 首次落库 2 行", conn.execute(
        "SELECT COUNT(*) FROM coin_rankings_ts").fetchone()[0] == 2)
    fa._store_rankings(conn, data, "top_gainers")  # 同 fetched_ts+type+symbol，跳过
    check("同轮去重仍 2 行", conn.execute(
        "SELECT COUNT(*) FROM coin_rankings_ts").fetchone()[0] == 2)
    fa._store_rankings(conn, data, "popular")  # 不同 type，新增
    check("不同 type 新增 2 行", conn.execute(
        "SELECT COUNT(*) FROM coin_rankings_ts").fetchone()[0] == 4)
    row = conn.execute(
        "SELECT * FROM coin_rankings_ts WHERE ranking_type='top_gainers' "
        "AND symbol='FLT'").fetchone()
    check("rankings 字段正确", row and row[1] == "top_gainers"
          and row[2] == "FLT" and abs(row[5] - 29.5) < 1e-6 and row[7] == 1, row)
    conn.close()
    os.remove(path)


def test_store_announcements():
    print("== announcements 落库 + notice_id 去重 + 噪音过滤 ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = {
        "items": [
            {"id": "282807", "title": "币安合约将上线 UNITREEUSDT U本位永续合约",
             "content": "", "platform": "binance", "notice_type": "1",
             "classify_name": "数字货币及交易对上新", "publish_time": "1787644858",
             "url": "https://www.binance.com/zh-CN/support/announcement/detail/x"},
            {"id": "b9f529199ca718d1", "title": "Announcement Center",
             "content": "页面导航噪音", "platform": "hotcoin-global",
             "notice_type": "99", "publish_time": "1787817600", "url": ""},
            {"id": "", "title": "", "platform": "binance", "notice_type": "1",
             "publish_time": "", "url": ""},
        ],
    }
    fa._store_announcements(conn, data)
    check("announcements 落库 1 行（噪音/空过滤）", conn.execute(
        "SELECT COUNT(*) FROM exchange_announcements").fetchone()[0] == 1)
    fa._store_announcements(conn, data)  # 同 notice_id，跳过
    check("同 notice_id 去重仍 1 行", conn.execute(
        "SELECT COUNT(*) FROM exchange_announcements").fetchone()[0] == 1)
    row = conn.execute("SELECT * FROM exchange_announcements").fetchone()
    check("announcements 字段正确", row and row[1] == "282807"
          and row[2].startswith("币安合约") and row[4] == "binance", row)
    conn.close()
    os.remove(path)


def test_store_xposts():
    print("== social 落库 + post_id 去重（social_posts_ts）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = {
        "items": [
            {"id": "a9855bf086b3452385e0babe04ecaa94", "author": "Solana Foundation",
             "channel": "SolanaFndn", "platform": "twitter", "content": "Solana Ramp",
             "upvotes": 23, "comment_count": 6, "sentiment_label": "bullish",
             "sentiment_score": 0.4, "quality_tier": "A",
             "create_time": "2026-08-20T16:31:16Z",
             "url": "https://x.com/SolanaFndn/status/2090476774203556069"},
            {"id": "0b8e3137be344c1b9cfbdce069b1c196", "author": "",
             "channel": "", "platform": "gate", "content": "SOL steady green",
             "upvotes": 0, "comment_count": 0, "sentiment_label": "bullish",
             "sentiment_score": 0.75, "quality_tier": "A",
             "create_time": "2026-08-20T15:02:25Z", "url": ""},
        ],
    }
    fa._store_xposts(conn, data, "SOL")
    check("social 首次落库 2 行", conn.execute(
        "SELECT COUNT(*) FROM social_posts_ts").fetchone()[0] == 2)
    fa._store_xposts(conn, data, "SOL")  # 同 post_id，跳过
    check("同 post_id 去重仍 2 行", conn.execute(
        "SELECT COUNT(*) FROM social_posts_ts").fetchone()[0] == 2)
    fa._store_xposts(conn, data, "BTC")  # 不同 coin 但同 post_id，仍跳过（post_id 全局唯一）
    check("跨币同 post_id 仍去重", conn.execute(
        "SELECT COUNT(*) FROM social_posts_ts").fetchone()[0] == 2)
    row = conn.execute(
        "SELECT * FROM social_posts_ts WHERE post_id='a9855bf086b3452385e0babe04ecaa94'"
    ).fetchone()
    check("social 字段正确", row and row[1] == "SOL"
          and row[4] == "SolanaFndn" and row[5] == "twitter"
          and row[7] == 23 and row[9] == "bullish"
          and abs(row[10] - 0.4) < 1e-6, row)
    conn.close()
    os.remove(path)


def test_store_coin_info():
    print("== coin_info 落库 + CEX 优先 + 复合主键去重（coin_info_ts）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = {
        "items": [
            {"symbol": "BTC", "name": json.dumps({"en_name": "Bitcoin", "cn_name": "比特币"}),
             "chain_flat": "BTC", "token_address": "", "category_type_flat": "Layer1",
             "exchange_type": "CEX", "market_value": "1300000000000",
             "sentiment_score": "0.62", "part_date": "2026-08-25"},
            {"symbol": "BTC", "name": "Bitcoin (DEX)", "chain_flat": "Solana",
             "token_address": "0xabc", "category_type_flat": "Meme",
             "exchange_type": "DEX", "market_value": "100", "sentiment_score": "0.1",
             "part_date": "2026-08-25"},
        ],
    }
    fa._store_coin_info(conn, data, "BTC")
    check("coin_info 落库 1 行（CEX 优先）", conn.execute(
        "SELECT COUNT(*) FROM coin_info_ts").fetchone()[0] == 1)
    row = conn.execute("SELECT * FROM coin_info_ts").fetchone()
    check("coin_info 字段正确", row and row[1] == "BTC"
          and row[2] == "Bitcoin" and row[3] == "比特币"
          and row[4] == "BTC" and row[7] == "CEX"
          and abs(row[8] - 1300000000000) < 1, row)
    fa._store_coin_info(conn, data, "BTC")  # 同 fetched_ts+symbol，REPLACE 仍 1 行
    check("同轮去重仍 1 行", conn.execute(
        "SELECT COUNT(*) FROM coin_info_ts").fetchone()[0] == 1)
    conn.close()
    os.remove(path)


def test_store_tech_analysis():
    print("== tech_analysis 落库 + 复合主键去重（tech_analysis_ts）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = {
        "symbol": "BTC_USDT", "period": "1d", "signal": "buy",
        "timeframes": {"15m": {"rsi": 55}, "1h": {"rsi": 60}},
    }
    fa._store_tech_analysis(conn, data, "BTC_USDT")
    check("tech_analysis 落库 1 行", conn.execute(
        "SELECT COUNT(*) FROM tech_analysis_ts").fetchone()[0] == 1)
    fa._store_tech_analysis(conn, data, "BTC_USDT")  # 同 fetched_ts+symbol，REPLACE 仍 1 行
    check("同轮去重仍 1 行", conn.execute(
        "SELECT COUNT(*) FROM tech_analysis_ts").fetchone()[0] == 1)
    row = conn.execute("SELECT * FROM tech_analysis_ts").fetchone()
    check("tech_analysis 字段正确", row and row[1] == "BTC_USDT"
          and row[2] == "1d" and row[3] == "buy"
          and json.loads(row[4]).get("1h", {}).get("rsi") == 60, row)
    conn.close()
    os.remove(path)


def test_store_onchain():
    print("== onchain 落库 + 复合主键去重（onchain_ts）==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    data = {
        "token": "ETH", "chain": "eth", "data_quality": "high",
        "activity": {
            "daily_active_addresses": 421000,
            "daily_transfer_volume": 1.2e9,
            "new_address_count_7d": 150000,
        },
        "holders": {"holder_count": 280000000},
    }
    fa._store_onchain(conn, data, "ETH")
    check("onchain 首次落库 1 行", conn.execute(
        "SELECT COUNT(*) FROM onchain_ts").fetchone()[0] == 1)
    fa._store_onchain(conn, data, "ETH")  # 同 fetched_ts+token，REPLACE 仍 1 行
    check("同轮去重仍 1 行", conn.execute(
        "SELECT COUNT(*) FROM onchain_ts").fetchone()[0] == 1)
    row = conn.execute("SELECT * FROM onchain_ts").fetchone()
    check("onchain 字段正确", row and row[1] == "ETH" and row[2] == "eth"
          and row[3] == 421000 and abs(row[4] - 1.2e9) < 1
          and row[5] == 150000 and row[6] == 280000000
          and row[7] == "high", row)
    conn.close()
    os.remove(path)


def test_store_event_signals():
    print("== event_signals 落库 + (event_ref,window) 去重 ==")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = make_db(path)
    signals = [{
        "event_identity": {"event_ref": "evt-1", "venue_event_title": "BTC 年底价格"},
        "window": "2026-12-31",
        "outcome_probabilities": {"above_100k": 0.6, "below_100k": 0.4},
        "directional_context": {"trend": "bullish"},
        "volume_flow": {"buy_volume": 100, "sell_volume": 50},
    }]
    fa._store_event_signals(conn, signals)
    check("event_signals 首次落库 1 行", conn.execute(
        "SELECT COUNT(*) FROM event_signals").fetchone()[0] == 1)
    fa._store_event_signals(conn, signals)  # 同 event_ref+window，REPLACE 仍 1 行
    check("同 event_ref+window 去重仍 1 行", conn.execute(
        "SELECT COUNT(*) FROM event_signals").fetchone()[0] == 1)
    row = conn.execute("SELECT * FROM event_signals").fetchone()
    check("event_signals 字段正确", row and row[1] == "evt-1"
          and row[2] == "BTC 年底价格" and row[3] == "2026-12-31"
          and json.loads(row[4]).get("above_100k") == 0.6
          and json.loads(row[5]).get("trend") == "bullish"
          and json.loads(row[6]).get("buy_volume") == 100, row)
    # 不同 window 视为不同信号，新增
    signals[0]["window"] = "2027-12-31"
    fa._store_event_signals(conn, signals)
    check("不同 window 新增 1 行", conn.execute(
        "SELECT COUNT(*) FROM event_signals").fetchone()[0] == 2)
    conn.close()
    os.remove(path)


if __name__ == "__main__":
    test_event_tiered_purge()
    test_ts_ttl()
    test_boundary_exact_ttl()
    test_schema_migration()
    test_schema_v8_to_v9_migration()
    test_macro_events_purge()
    test_unknown_level_fallback()
    test_collect_db_tables()
    test_store_sentiment()
    test_store_reserves()
    test_store_stats()
    test_store_liquidations()
    test_store_orderbook()
    test_store_trades()
    test_store_rankings()
    test_store_announcements()
    test_store_xposts()
    test_store_coin_info()
    test_store_tech_analysis()
    test_store_onchain()
    test_store_event_signals()
    print("\n结果: %d 通过, %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)