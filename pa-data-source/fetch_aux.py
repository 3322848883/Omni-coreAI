"""aux-info-feed 采集脚本（旁路辅助信息流，非核心价格行为分析）

与 kline_watcher.py 并列，由同一 watchdog 守护（整体重启）。
数据全部落在本目录 aux-data/ 子目录，与 kline.db / account.db 严格隔离：
  - snapshot/       落盘 JSON（events/overview/macro/sentiment/reserves/stats/liquidations/orderbook/trades/rankings/announcements/social/coin_info/tech_analysis/onchain/event_signals）
  - aux_cache.db    独立滚动缓存库（唯一自建、唯一自用的 SQLite）
  - seen_ids.json   事件去重
  - fetch_schedule.json 各接口上次采集时间（频率调度）
  - aux_status.json 采集状态（健康可见，供后续消费）
  - logs/           独立日志

用法:
    python fetch_aux.py            # 单次采集
    python fetch_aux.py --loop     # 常驻循环（默认 5s 间隔，由 watchdog 守护）
"""

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from logging.handlers import TimedRotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "aux-data")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "snapshot")
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "aux_info_feed.log")
DB_PATH = os.path.join(DATA_DIR, "aux_cache.db")
SEEN_PATH = os.path.join(DATA_DIR, "seen_ids.json")
LOCK_FILE = os.path.join(DATA_DIR, "aux_fetch.lock")
STATUS_PATH = os.path.join(DATA_DIR, "aux_status.json")

GATE_CLI_DIR = os.environ.get("GATE_CLI_DIR", BASE_DIR)
GATE_CLI = os.path.join(
    GATE_CLI_DIR, "gate-cli.exe" if sys.platform == "win32" else "gate-cli"
)

# 期货公共行情直连（gate-cli 的 HTTP 客户端被网关风控重置连接，见_fetch_futures_public）
FUTURES_PUBLIC_BASE = "https://api.gateio.ws/api/v4/futures/usdt"
_FUTURES_UA = "HermesAUX/1.0"

# 事件级别 -> 清理周期（秒），硬上限 24h
EVENT_TTL = {
    "major_even": 24 * 3600,
    "market_move": 12 * 3600,
    "coin_move": 6 * 3600,
}
DEFAULT_EVENT_TTL = 6 * 3600

# 时序表 -> 清理周期（秒）
TS_TTL = {
    "overview_ts": 30 * 86400,
    "macro_ts": 90 * 86400,
    "sentiment_ts": 14 * 86400,
    "reserves_ts": 90 * 86400,
    "market_stats_ts": 90 * 86400,
    "liquidations": 24 * 3600,
    "orderbook_snap": 3600,
    "trades": 24 * 3600,
    "coin_rankings_ts": 7 * 86400,
    "exchange_announcements": 7 * 86400,
    "social_posts_ts": 3 * 86400,
    "coin_info_ts": 30 * 86400,
    "tech_analysis_ts": 7 * 86400,
    "onchain_ts": 30 * 86400,
    "event_signals": 7 * 86400,
}

# 缓存表 schema 版本：变更表结构时 +1，旧库自动重建（缓存不长期保存，重建可接受）。
# v8→v9 例外：trades 主键改复合键走定向迁移（数据保留），其余表不动。
SCHEMA_VERSION = 9

SEEN_LIMIT = 20000

# 循环粒度 5s：支撑订单簿/成交秒级采集；低频接口由 FETCH_INTERVAL 门控拦截
LOOP_INTERVAL = 5
# 维护操作（purge）间隔，避免每次循环浪费
MAINT_INTERVAL = 60

# 合约市场结构数据按品种采集（与 watchlist.yaml symbols 一致）
CONTRACTS = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XAU_USDT", "XAG_USDT"]

# 社区舆情查询词（symbol + 名称提升召回；XAU/XAG 为贵金属，用 gold/silver）
XPOST_QUERIES = {
    "BTC_USDT": "BTC Bitcoin",
    "ETH_USDT": "ETH Ethereum",
    "SOL_USDT": "SOL Solana",
    "XAU_USDT": "XAU gold",
    "XAG_USDT": "XAG silver",
}

# 币种基本面查询符号：仅加密合约（BTC/ETH/SOL）。
# XAU/XAG 为贵金属，get-coin-info 实测返回无关 meme 代币（如 Solana 上名为 Gold 的代币），
# 无有效币种信息，故不采集，见 PLAN-intel-feed.md §5.2 实施修正
COIN_INFO_SYMBOLS = ["BTC", "ETH", "SOL"]

# 链上数据查询符号：仅 ETH。
# BTC/SOL 实测返回与 ETH 完全相同的链上总览（chain=eth、同一组 activity），
# token 参数对原生币不生效（统一回 chain_overview），采集会误导，故不采集，
# 见 PLAN-intel-feed.md §6.1 实施修正
ONCHAIN_TOKENS = ["ETH"]

# 事件信号每轮取前 N 个加密预测事件（按 attention_score 降序），避免命令数膨胀
EVENT_SIGNAL_LIMIT = 5

# 各接口采集频率（秒），与 DESIGN §5.5 调度表一致
FETCH_INTERVAL = {
    "events": 1800,      # 30min
    "overview": 3600,    # 1h
    "macro": 12 * 3600,  # 每日 1-2 次
    "sentiment": 3600,   # 1h
    "reserves": 86400,   # 每日 1 次
    "stats": 3600,       # 1h（持仓量/多空比变化慢）
    "liquidations": 300, # 5m（事件型聚合）
    "orderbook": 5,      # 5s（瞬时盘口）
    "trades": 5,         # 5s（实时成交）
    "rankings": 900,     # 15min（市场热度榜）
    "announcements": 900,# 15min（上币/下架公告）
    "social": 1800,      # 30min（社区舆情，search-ugc 多平台社交帖）
    "coin_info": 21600,  # 6h（币种基本面，静态信息）
    "tech_analysis": 1800, # 30min（技术面情报，多周期指标）
    "onchain": 21600,    # 6h（链上数据，静态信息）
    "event_signals": 1800, # 30min（事件信号，预测类）
}
SCHEDULE_PATH = os.path.join(DATA_DIR, "fetch_schedule.json")

running = True
log = logging.getLogger("aux-info-feed")


def _pid_alive(pid):
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def acquire_single_instance_lock():
    """单实例锁：防止双实例写同一 SQLite（与 kline_watcher 同模式）"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(LOCK_FILE):
        try:
            old = int(open(LOCK_FILE, encoding="utf-8").read().strip())
            if old > 0 and _pid_alive(old):
                return None
        except Exception:
            pass
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
    try:
        h = open(LOCK_FILE, "x", encoding="utf-8")
        h.write(str(os.getpid()))
        h.flush()
        return h
    except FileExistsError:
        return None


def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    log.addHandler(console)
    file_handler = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)


def signal_handler(sig, frame):
    global running
    log.info("收到退出信号 %s, 优雅退出...", sig)
    running = False


def _run_cli(args, timeout=30):
    cmd = [GATE_CLI] + args + ["--format", "json"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=GATE_CLI_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        log.error("gate-cli 超时: %s", " ".join(args))
        return None
    except Exception as e:
        log.error("gate-cli 执行异常: %s", e)
        return None
    if result.returncode != 0:
        log.error("gate-cli 失败: %s -> %s", " ".join(args), result.stderr.strip())
        return None
    output = result.stdout.strip()
    if not output:
        log.error("gate-cli 输出为空: %s", " ".join(args))
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        log.error("gate-cli 输出 JSON 解析失败: %s -> %s", " ".join(args), e)
        return None


def _fetch_futures_public(path, params=None):
    """期货公共行情直连。

    gate-cli 的 Go HTTP 客户端特征被网关对订单簿/成交等高频行情接口风控，
    稳定 500 连接重置；标准库 urllib（同网络 curl/Python 已验证 200）可绕过，
    返回与 gate-cli 透传一致的原始 JSON。"""
    url = FUTURES_PUBLIC_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _FUTURES_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.error("futures REST %s 失败: %s", path, e)
        return None


def _fetch_events():
    return _run_cli(["news", "events", "get-latest-events"])


def _fetch_overview():
    return _run_cli(["info", "marketsnapshot", "get-market-overview"])


def _fetch_macro():
    return _run_cli(["info", "macro", "get-macro-summary"])


def _fetch_sentiment():
    return _run_cli(["news", "feed", "get-social-sentiment", "--coin", "BTC"])


def _fetch_reserves():
    return _run_cli(["info", "platformmetrics", "get-exchange-reserves", "--asset", "BTC"])


def _f(v):
    """字符串/数值安全转 float，失败返回 None（合约接口部分字段为字符串）"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_stats(contract):
    return _fetch_futures_public("/contract_stats",
                                 {"contract": contract, "interval": "1h", "limit": "1"})


def _fetch_liquidations(contract):
    return _fetch_futures_public("/liq_orders",
                                 {"contract": contract, "limit": "50"})


def _fetch_orderbook(contract):
    return _fetch_futures_public("/order_book",
                                 {"contract": contract, "limit": "5"})


def _fetch_trades(contract):
    return _fetch_futures_public("/trades",
                                 {"contract": contract, "limit": "50"})


# 市场热度榜类型（gate-cli info coin get-coin-rankings 允许值）
# 注：new_listing 榜返回结构与其他榜不同（无 symbol，币名嵌套在 metadata.coin_name，
# 本质是交易所公告），已由 exchange_announcements 接口覆盖，故不纳入
RANKING_TYPES = ["popular", "top_gainers", "top_losers",
                 "twitter_hot", "market_pulse_hot"]


def _fetch_rankings(rtype):
    return _run_cli(["info", "coin", "get-coin-rankings",
                     "--ranking-type", rtype, "--limit", "10"])


def _fetch_announcements():
    return _run_cli(["news", "feed", "get-exchange-announcements",
                     "--platform", "binance", "--limit", "20"])


# 社区舆情：search-ugc 多平台社交帖（gate/twitter/telegram/reddit）。
# 注：原方案 search-x 实测 items 恒空（xAI 路径只回聚合摘要无原文），
# 改用 search-ugc 拿真实帖子原文 + 逐帖情绪，见 PLAN-intel-feed.md §5.1 实施修正
def _fetch_xposts(query):
    return _run_cli(["news", "feed", "search-ugc",
                     "--query", query, "--limit", "5"])


def _fetch_coin_info(symbol):
    return _run_cli(["info", "coin", "get-coin-info",
                     "--query", symbol, "--scope", "full"])


def _fetch_tech_analysis(symbol):
    return _run_cli(["info", "markettrend", "get-technical-analysis",
                     "--symbol", symbol])


def _fetch_onchain(token):
    return _run_cli(["info", "onchain", "get-token-onchain",
                     "--token", token, "--scope", "full"])


# 事件信号（P2）：search-events 发现加密预测事件（category=crypto_price 过滤，
# 避免默认返回 Polymarket 政治数据）→ 逐个 get-event-signal。
# 按 attention_score 降序取前 EVENT_SIGNAL_LIMIT 个，控制命令数
def _fetch_event_signals():
    data = _run_cli(["news", "prediction", "search-events",
                     "--category", "crypto_price", "--limit", "20"])
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    events = sorted(
        data["events"],
        key=lambda e: e.get("attention_score") or 0,
        reverse=True,
    )
    signals = []
    for ev in events[:EVENT_SIGNAL_LIMIT]:
        ref = ev.get("event_ref")
        if not ref:
            continue
        sig = _run_cli(["news", "prediction", "get-event-signal",
                        "--event-ref", ref])
        if isinstance(sig, dict):
            signals.append(sig)
    return signals


def _write_snapshot(name, source, data):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    payload = {
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "fetched_ts": int(time.time()),
        "source": source,
        "status": "ok" if data is not None else "fail",
    }
    if isinstance(data, dict):
        payload.update(data)
    else:
        payload["data"] = data
    path = os.path.join(SNAPSHOT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _load_seen():
    if not os.path.exists(SEEN_PATH):
        return {}
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_seen(seen):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False)


def _load_schedule():
    if not os.path.exists(SCHEDULE_PATH):
        return {}
    try:
        with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_schedule(sched):
    try:
        with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
            json.dump(sched, f, ensure_ascii=False)
    except OSError as e:
        log.error("保存调度状态失败: %s", e)


def _due(key, sched, now):
    return (now - sched.get(key, 0)) >= FETCH_INTERVAL[key]


def _migrate_trades_composite_pk(conn):
    """v8→v9：trades 主键 (trade_id) → (trade_id, contract)，数据保留迁移。

    旧表改名 → 建新表 → 回灌（同 id 同合约经 IGNORE 去重，跨合约同 id 保留）→
    删旧表 → 重建 time 索引；整体一个事务，中途失败自动回滚不留半迁移态。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not exists:
        return
    try:
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS trades_old_v8")
        conn.execute("ALTER TABLE trades RENAME TO trades_old_v8")
        conn.execute("""CREATE TABLE trades (
            trade_id INTEGER,
            time INTEGER,
            contract TEXT,
            price REAL,
            size REAL,
            fetched_ts INTEGER,
            PRIMARY KEY (trade_id, contract)
        )""")
        conn.execute(
            "INSERT OR IGNORE INTO trades (trade_id, time, contract, price, size, fetched_ts) "
            "SELECT trade_id, time, contract, price, size, fetched_ts FROM trades_old_v8")
        conn.execute("DROP TABLE trades_old_v8")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_time ON trades (time)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _init_db(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    version = int(cur[0]) if cur and cur[0] else 0
    if version < SCHEMA_VERSION:
        if version == 8:
            # v8→v9 定向迁移：仅 trades 主键 trade_id → (trade_id, contract)。
            # Gate 各合约成交 ID 独立编号，单字段主键跨合约碰撞时
            # INSERT OR IGNORE 会静默丢数据（实测 v8 库五合约区间暂未相交，
            # 属潜伏隐患）。数据保留迁移，其余 16 张表不动。
            _migrate_trades_composite_pk(conn)
        else:
            for t in ("events", "overview_ts", "macro_ts", "sentiment_ts", "reserves_ts", "macro_events",
                      "market_stats_ts", "liquidations", "orderbook_snap", "trades",
                      "coin_rankings_ts", "exchange_announcements", "social_posts_ts",
                      "coin_info_ts", "tech_analysis_ts", "onchain_ts", "event_signals"):
                conn.execute("DROP TABLE IF EXISTS %s" % t)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT,
        event_time TEXT,
        event_title TEXT,
        impact_direction TEXT,
        impact_direction_reason TEXT,
        impact_analysis TEXT,
        price_change TEXT,
        context TEXT,
        related_coins TEXT,
        tags TEXT,
        fetched_ts INTEGER
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_events_fetched ON events (fetched_ts)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS overview_ts (
        fetched_ts INTEGER PRIMARY KEY,
        btc_price REAL,
        btc_dominance REAL,
        eth_dominance REAL,
        fear_greed INTEGER,
        fear_greed_label TEXT,
        total_market_cap REAL,
        total_volume_24h REAL,
        market_cap_change_24h REAL,
        altcoin_season_index REAL,
        ahr999 REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS macro_ts (
        fetched_ts INTEGER PRIMARY KEY,
        cpi_yoy REAL,
        fed_funds_rate REAL,
        gdp_growth REAL,
        nonfarm_payroll REAL,
        pce_yoy REAL,
        unemployment_rate REAL,
        raw TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS macro_events (
        event_id TEXT PRIMARY KEY,
        event_date TEXT,
        event_name TEXT,
        event_type TEXT,
        event_time TEXT,
        status TEXT,
        fetched_ts INTEGER
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_macro_events_date ON macro_events (event_date)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sentiment_ts (
        fetched_ts INTEGER PRIMARY KEY,
        coin TEXT,
        mention_count INTEGER,
        overall_sentiment REAL,
        sentiment_label TEXT,
        positive_ratio REAL,
        neutral_ratio REAL,
        negative_ratio REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reserves_ts (
        fetched_ts INTEGER PRIMARY KEY,
        asset TEXT,
        reserve_amount REAL,
        reserve_usd REAL,
        change_24h_pct REAL,
        change_7d_pct REAL,
        change_30d_pct REAL,
        snapshot_time TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS market_stats_ts (
        fetched_ts INTEGER,
        contract TEXT,
        lsr_taker REAL,
        lsr_account REAL,
        long_liq_size REAL,
        short_liq_size REAL,
        open_interest REAL,
        open_interest_usd REAL,
        top_lsr_account REAL,
        top_lsr_size REAL,
        mark_price REAL,
        PRIMARY KEY (fetched_ts, contract)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS liquidations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time INTEGER,
        contract TEXT,
        size REAL,
        order_size REAL,
        order_price REAL,
        fill_price REAL,
        fetched_ts INTEGER
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_liq_time ON liquidations (time)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orderbook_snap (
        fetched_ts INTEGER,
        contract TEXT,
        bids TEXT,
        asks TEXT,
        PRIMARY KEY (fetched_ts, contract)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        trade_id INTEGER,
        time INTEGER,
        contract TEXT,
        price REAL,
        size REAL,
        fetched_ts INTEGER,
        PRIMARY KEY (trade_id, contract)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_trades_time ON trades (time)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS coin_rankings_ts (
        fetched_ts INTEGER,
        ranking_type TEXT,
        symbol TEXT,
        name TEXT,
        latest_price REAL,
        price_change_24h REAL,
        market_cap REAL,
        rank INTEGER,
        PRIMARY KEY (fetched_ts, ranking_type, symbol)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS exchange_announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notice_id TEXT,
        title TEXT,
        content TEXT,
        platform TEXT,
        notice_type TEXT,
        classify_name TEXT,
        publish_time TEXT,
        url TEXT,
        fetched_ts INTEGER,
        UNIQUE (notice_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_announce_publish ON exchange_announcements (publish_time)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS social_posts_ts (
        fetched_ts INTEGER,
        coin TEXT,
        post_id TEXT,
        author TEXT,
        channel TEXT,
        platform TEXT,
        content TEXT,
        upvotes INTEGER,
        comment_count INTEGER,
        sentiment_label TEXT,
        sentiment_score REAL,
        quality_tier TEXT,
        created_time TEXT,
        url TEXT,
        PRIMARY KEY (post_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_social_coin ON social_posts_ts (coin, fetched_ts)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS coin_info_ts (
        fetched_ts INTEGER,
        symbol TEXT,
        name TEXT,
        name_cn TEXT,
        chain TEXT,
        contract_address TEXT,
        category TEXT,
        exchange_type TEXT,
        market_value REAL,
        sentiment_score REAL,
        part_date TEXT,
        PRIMARY KEY (fetched_ts, symbol)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tech_analysis_ts (
        fetched_ts INTEGER,
        symbol TEXT,
        period TEXT,
        signal TEXT,
        timeframes_json TEXT,
        PRIMARY KEY (fetched_ts, symbol)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS onchain_ts (
        fetched_ts INTEGER,
        token TEXT,
        chain TEXT,
        daily_active_addresses INTEGER,
        daily_transfer_volume REAL,
        new_address_count_7d INTEGER,
        holder_count INTEGER,
        data_quality TEXT,
        PRIMARY KEY (fetched_ts, token)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS event_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_ref TEXT,
        event_name TEXT,
        window TEXT,
        outcome_probabilities TEXT,
        directional_context TEXT,
        volume_flow TEXT,
        fetched_ts INTEGER,
        UNIQUE (event_ref, window)
    )""")
    conn.commit()
    return conn


def _store_events(conn, data, seen):
    if not data or not isinstance(data.get("items"), list):
        return 0
    new = 0
    now = int(time.time())
    events_seen = seen.setdefault("events", [])
    seen_set = set(events_seen)
    for it in data["items"]:
        eid = it.get("event_id")
        if not eid:
            continue
        cur = conn.execute(
            "SELECT 1 FROM events WHERE event_id=?", (eid,)
        ).fetchone()
        if cur is None:
            pc = it.get("price_change")
            if isinstance(pc, list):
                pc = json.dumps(pc, ensure_ascii=False)
            conn.execute(
                "INSERT OR IGNORE INTO events "
                "(event_id, event_type, event_time, event_title, impact_direction, "
                " impact_direction_reason, impact_analysis, price_change, context, "
                " related_coins, tags, fetched_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    it.get("event_type", ""),
                    it.get("event_time", ""),
                    it.get("event_title", ""),
                    it.get("impact_direction", ""),
                    it.get("impact_direction_reason", ""),
                    it.get("impact_analysis", ""),
                    pc,
                    it.get("context", ""),
                    json.dumps(it.get("related_coins") or [], ensure_ascii=False),
                    json.dumps(it.get("tags") or [], ensure_ascii=False),
                    now,
                ),
            )
            new += 1
        if eid not in seen_set:
            events_seen.append(eid)
            seen_set.add(eid)
    if len(events_seen) > SEEN_LIMIT:
        del events_seen[: len(events_seen) - SEEN_LIMIT]
    conn.commit()
    return new


def _store_overview(conn, data):
    if not isinstance(data, dict):
        return
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO overview_ts "
        "(fetched_ts, btc_price, btc_dominance, eth_dominance, fear_greed, fear_greed_label, "
        " total_market_cap, total_volume_24h, market_cap_change_24h, "
        " altcoin_season_index, ahr999) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            now,
            data.get("btc_price"),
            data.get("btc_dominance"),
            data.get("eth_dominance"),
            data.get("fear_greed_index"),
            data.get("fear_greed_label"),
            data.get("total_market_cap"),
            data.get("total_volume_24h"),
            data.get("market_cap_change_24h"),
            data.get("altcoin_season_index"),
            data.get("ahr999"),
        ),
    )
    conn.commit()


def _store_macro(conn, data):
    if not isinstance(data, dict):
        return
    ind = data.get("indicators") or {}
    def _v(key):
        v = ind.get(key)
        return v.get("value") if isinstance(v, dict) else None

    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO macro_ts "
        "(fetched_ts, cpi_yoy, fed_funds_rate, gdp_growth, nonfarm_payroll, "
        " pce_yoy, unemployment_rate, raw) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            now,
            _v("cpi_yoy"),
            _v("federal_funds_rate"),
            _v("gdp_growth"),
            _v("nonfarm_payroll"),
            _v("pce_yoy"),
            _v("unemployment_rate"),
            json.dumps(ind, ensure_ascii=False),
        ),
    )
    for ev in data.get("next_events") or []:
        eid = ev.get("event_id")
        if not eid:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO macro_events "
            "(event_id, event_date, event_name, event_type, event_time, status, fetched_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                eid,
                ev.get("event_date", ""),
                ev.get("event_name", ""),
                ev.get("event_type", ""),
                ev.get("event_time", ""),
                ev.get("status", ""),
                now,
            ),
        )
    conn.commit()


def _store_sentiment(conn, data):
    if not isinstance(data, dict):
        return
    dist = data.get("sentiment_distribution") or {}
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO sentiment_ts "
        "(fetched_ts, coin, mention_count, overall_sentiment, sentiment_label, "
        " positive_ratio, neutral_ratio, negative_ratio) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            now,
            data.get("coin", ""),
            data.get("mention_count"),
            data.get("overall_sentiment"),
            data.get("sentiment_label", ""),
            dist.get("positive_ratio"),
            dist.get("neutral_ratio"),
            dist.get("negative_ratio"),
        ),
    )
    conn.commit()


def _store_reserves(conn, data):
    if not isinstance(data, dict):
        return
    items = data.get("items") or []
    if not items:
        return
    item = items[0]
    snap = item.get("snapshot_time", "")
    last = conn.execute(
        "SELECT snapshot_time FROM reserves_ts ORDER BY fetched_ts DESC LIMIT 1"
    ).fetchone()
    if last and last[0] == snap:
        return  # 同一快照（每日更新），跳过避免重复行
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO reserves_ts "
        "(fetched_ts, asset, reserve_amount, reserve_usd, change_24h_pct, "
        " change_7d_pct, change_30d_pct, snapshot_time) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            now,
            item.get("asset", ""),
            item.get("reserve_amount"),
            item.get("reserve_usd"),
            item.get("change_24h_pct"),
            item.get("change_7d_pct"),
            item.get("change_30d_pct"),
            snap,
        ),
    )
    conn.commit()


def _store_stats(conn, data, contract):
    if not isinstance(data, list) or not data:
        return
    item = data[0]
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO market_stats_ts "
        "(fetched_ts, contract, lsr_taker, lsr_account, long_liq_size, short_liq_size, "
        " open_interest, open_interest_usd, top_lsr_account, top_lsr_size, mark_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            now,
            contract,
            item.get("lsr_taker"),
            item.get("lsr_account"),
            _f(item.get("long_liq_size")),
            _f(item.get("short_liq_size")),
            _f(item.get("open_interest")),
            item.get("open_interest_usd"),
            item.get("top_lsr_account"),
            _f(item.get("top_lsr_size")),
            item.get("mark_price"),
        ),
    )
    conn.commit()


def _store_liquidations(conn, data, contract):
    if not isinstance(data, list):
        return
    now = int(time.time())
    new = 0
    for it in data:
        t = it.get("time")
        if not t:
            continue
        cur = conn.execute(
            "SELECT 1 FROM liquidations WHERE time=? AND contract=?",
            (t, contract),
        ).fetchone()
        if cur is not None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO liquidations "
            "(time, contract, size, order_size, order_price, fill_price, fetched_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                t,
                contract,
                _f(it.get("size")),
                _f(it.get("order_size")),
                _f(it.get("order_price")),
                _f(it.get("fill_price")),
                now,
            ),
        )
        new += 1
    conn.commit()
    return new


def _store_orderbook(conn, data, contract):
    if not isinstance(data, dict):
        return
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO orderbook_snap (fetched_ts, contract, bids, asks) "
        "VALUES (?,?,?,?)",
        (
            now,
            contract,
            json.dumps(data.get("bids") or [], ensure_ascii=False),
            json.dumps(data.get("asks") or [], ensure_ascii=False),
        ),
    )
    conn.commit()


def _store_trades(conn, data, contract):
    if not isinstance(data, list):
        return
    now = int(time.time())
    # total_changes 差值=真实插入数：每 5s 轮询大量重复成交被 IGNORE，
    # 此前无条件 new+=1 使日志"新增"口径远大于实际入库
    before = conn.total_changes
    for it in data:
        tid = it.get("id")
        if not tid:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(trade_id, time, contract, price, size, fetched_ts) "
            "VALUES (?,?,?,?,?,?)",
            (
                tid,
                it.get("create_time"),
                contract,
                _f(it.get("price")),
                _f(it.get("size")),
                now,
            ),
        )
    conn.commit()
    return conn.total_changes - before


def _store_rankings(conn, data, rtype):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return 0
    now = int(time.time())
    new = 0
    for it in data["items"]:
        symbol = it.get("symbol")
        if not symbol:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO coin_rankings_ts "
            "(fetched_ts, ranking_type, symbol, name, latest_price, "
            " price_change_24h, market_cap, rank) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                now,
                rtype,
                symbol,
                it.get("name", ""),
                _f(it.get("latest_price")),
                _f(it.get("price_change_24h")),
                _f(it.get("market_cap")),
                it.get("rank"),
            ),
        )
        new += 1
    conn.commit()
    return new


# 公告页面导航噪音标题（混入 items 的页面文本，非真实公告）
_ANNOUNCE_NOISE = {"Announcement Center"}


def _store_announcements(conn, data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return 0
    now = int(time.time())
    new = 0
    for it in data["items"]:
        nid = str(it.get("id", ""))
        title = (it.get("title") or "").strip()
        if not nid or not title or title in _ANNOUNCE_NOISE:
            continue
        cur = conn.execute(
            "SELECT 1 FROM exchange_announcements WHERE notice_id=?", (nid,)
        ).fetchone()
        if cur is not None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO exchange_announcements "
            "(notice_id, title, content, platform, notice_type, classify_name, "
            " publish_time, url, fetched_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                nid,
                title,
                it.get("content", ""),
                it.get("platform", ""),
                it.get("notice_type", ""),
                it.get("classify_name", ""),
                it.get("publish_time", ""),
                it.get("url", ""),
                now,
            ),
        )
        new += 1
    conn.commit()
    return new


def _store_xposts(conn, data, coin):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return 0
    now = int(time.time())
    # 同 _store_trades：post_id 单字段主键，重搜出的旧帖被 IGNORE 不计入
    before = conn.total_changes
    for it in data["items"]:
        pid = it.get("id")
        if not pid:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO social_posts_ts "
            "(fetched_ts, coin, post_id, author, channel, platform, content, "
            " upvotes, comment_count, sentiment_label, sentiment_score, "
            " quality_tier, created_time, url) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now,
                coin,
                pid,
                it.get("author", ""),
                it.get("channel", ""),
                it.get("platform", ""),
                it.get("content", ""),
                it.get("upvotes"),
                it.get("comment_count"),
                it.get("sentiment_label", ""),
                _f(it.get("sentiment_score")),
                it.get("quality_tier", ""),
                it.get("create_time", ""),
                it.get("url", ""),
            ),
        )
    conn.commit()
    return conn.total_changes - before


def _store_coin_info(conn, data, symbol):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return 0
    # 同 symbol 会返回多个条目（CEX 主条目 + 各链 DEX 变体），优先 CEX 主条目，
    # 避免 INSERT OR REPLACE 被 DEX 变体覆盖（同 fetched_ts+symbol 只留一行）
    matched = [it for it in data["items"] if it.get("symbol") == symbol]
    if not matched:
        return 0
    it = next((x for x in matched if x.get("exchange_type") == "CEX"), matched[0])
    now = int(time.time())
    name_raw = it.get("name")
    name, name_cn = "", ""
    if isinstance(name_raw, str):
        try:
            nm = json.loads(name_raw)
            name = nm.get("en_name", "")
            name_cn = nm.get("cn_name", "")
        except Exception:
            name = name_raw
    elif isinstance(name_raw, dict):
        name = name_raw.get("en_name", "")
        name_cn = name_raw.get("cn_name", "")
    conn.execute(
        "INSERT OR REPLACE INTO coin_info_ts "
        "(fetched_ts, symbol, name, name_cn, chain, contract_address, category, "
        " exchange_type, market_value, sentiment_score, part_date) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            now,
            symbol,
            name,
            name_cn,
            it.get("chain_flat", ""),
            it.get("token_address", ""),
            it.get("category_type_flat", ""),
            it.get("exchange_type", ""),
            _f(it.get("market_value")),
            _f(it.get("sentiment_score")),
            it.get("part_date", ""),
        ),
    )
    conn.commit()
    return 1


def _store_tech_analysis(conn, data, symbol):
    if not isinstance(data, dict):
        return 0
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO tech_analysis_ts "
        "(fetched_ts, symbol, period, signal, timeframes_json) "
        "VALUES (?,?,?,?,?)",
        (
            now,
            data.get("symbol", symbol),
            data.get("period", ""),
            data.get("signal", ""),
            json.dumps(data.get("timeframes") or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    return 1


def _store_onchain(conn, data, token):
    if not isinstance(data, dict):
        return 0
    now = int(time.time())
    act = data.get("activity") or {}
    holders = data.get("holders") or {}
    conn.execute(
        "INSERT OR REPLACE INTO onchain_ts "
        "(fetched_ts, token, chain, daily_active_addresses, daily_transfer_volume, "
        " new_address_count_7d, holder_count, data_quality) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            now,
            data.get("token", token),
            data.get("chain", ""),
            _f(act.get("daily_active_addresses")),
            _f(act.get("daily_transfer_volume")),
            _f(act.get("new_address_count_7d")),
            _f(holders.get("holder_count")),
            data.get("data_quality", ""),
        ),
    )
    conn.commit()
    return 1


def _store_event_signals(conn, signals):
    if not isinstance(signals, list):
        return 0
    now = int(time.time())
    new = 0
    for sig in signals:
        ident = sig.get("event_identity") or {}
        ref = ident.get("event_ref")
        if not ref:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO event_signals "
            "(event_ref, event_name, window, outcome_probabilities, "
            " directional_context, volume_flow, fetched_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                ref,
                ident.get("venue_event_title", ""),
                sig.get("window", ""),
                json.dumps(sig.get("outcome_probabilities") or {}, ensure_ascii=False),
                json.dumps(sig.get("directional_context") or {}, ensure_ascii=False),
                json.dumps(sig.get("volume_flow") or {}, ensure_ascii=False),
                now,
            ),
        )
        new += 1
    conn.commit()
    return new


def purge_old(conn):
    now = int(time.time())
    deleted = {}
    for level, ttl in EVENT_TTL.items():
        cur = conn.execute(
            "DELETE FROM events WHERE event_type=? AND fetched_ts < ?",
            (level, now - ttl),
        )
        deleted["events:" + level] = cur.rowcount
    known = list(EVENT_TTL.keys())
    placeholders = ",".join("?" * len(known))
    cur = conn.execute(
        "DELETE FROM events WHERE event_type NOT IN (%s) AND fetched_ts < ?"
        % placeholders,
        tuple(known) + (now - DEFAULT_EVENT_TTL,),
    )
    deleted["events:other"] = cur.rowcount
    for table, ttl in TS_TTL.items():
        cur = conn.execute(
            "DELETE FROM %s WHERE fetched_ts < ?" % table,
            (now - ttl,),
        )
        deleted[table] = cur.rowcount
    cur = conn.execute(
        "DELETE FROM macro_events WHERE event_date < ?",
        (time.strftime("%Y-%m-%d", time.localtime()),),
    )
    deleted["macro_events"] = cur.rowcount
    conn.commit()
    log.info("purge 完成: %s", deleted)
    return deleted


def _collect_db_tables(conn):
    """采集后汇总滚动缓存表原始状态（计数/最新时间/版本），供监控消费（零判断）"""
    tables = {}
    for t in ("events", "overview_ts", "macro_ts", "macro_events", "sentiment_ts", "reserves_ts",
              "market_stats_ts", "liquidations", "orderbook_snap", "trades",
              "coin_rankings_ts", "exchange_announcements", "social_posts_ts",
              "coin_info_ts", "tech_analysis_ts", "onchain_ts", "event_signals"):
        try:
            row = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()
            tables[t] = {"count": row[0] if row else 0}
        except Exception:
            tables[t] = {"count": -1}
    for t in ("overview_ts", "macro_ts", "sentiment_ts", "reserves_ts",
              "market_stats_ts", "orderbook_snap", "liquidations", "trades",
              "coin_rankings_ts", "exchange_announcements", "social_posts_ts",
              "coin_info_ts", "tech_analysis_ts", "onchain_ts", "event_signals"):
        try:
            row = conn.execute("SELECT MAX(fetched_ts) FROM %s" % t).fetchone()
            tables[t]["latest_ts"] = row[0] if row and row[0] is not None else None
        except Exception:
            tables[t]["latest_ts"] = None
    try:
        row = conn.execute("SELECT MAX(fetched_ts) FROM events").fetchone()
        tables["events"]["latest_ts"] = row[0] if row and row[0] is not None else None
    except Exception:
        tables["events"]["latest_ts"] = None
    try:
        row = conn.execute("SELECT MAX(event_date) FROM macro_events").fetchone()
        tables["macro_events"]["latest_date"] = row[0] if row and row[0] is not None else None
    except Exception:
        tables["macro_events"]["latest_date"] = None
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return {
        "schema_version": int(ver[0]) if ver and ver[0] else None,
        "tables": tables,
    }


def _write_status(interfaces, purge_deleted, db_tables):
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "present": True,
        "src": "aux-info-feed (fetch_aux.py)",
        "ts": int(time.time()),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "interfaces": interfaces,
        "purge": purge_deleted,
        "db_tables": db_tables,
        "db": os.path.relpath(DB_PATH, BASE_DIR),
    }
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def run_once():
    if not os.path.exists(GATE_CLI):
        log.error("gate-cli 不存在: %s", GATE_CLI)
        return
    conn = _init_db()
    seen = _load_seen()
    sched = _load_schedule()
    now = int(time.time())
    interfaces = {}
    try:
        if _due("events", sched, now):
            events = _fetch_events()
            _write_snapshot("events.json", "news/events/get-latest-events", events)
            n_new = _store_events(conn, events, seen)
            interfaces["events"] = {
                "status": "ok" if events is not None else "fail",
                "count": len(events.get("items", [])) if isinstance(events, dict) else 0,
                "new": n_new,
            }
            log.info("events: %s 条, 新增 %s 条", interfaces["events"]["count"], n_new)
            sched["events"] = now

        if _due("overview", sched, now):
            overview = _fetch_overview()
            _write_snapshot("overview.json", "info/marketsnapshot/get-market-overview", overview)
            _store_overview(conn, overview)
            interfaces["overview"] = {"status": "ok" if overview else "fail"}
            log.info("overview: %s", "ok" if overview else "fail")
            sched["overview"] = now

        if _due("macro", sched, now):
            macro = _fetch_macro()
            _write_snapshot("macro.json", "info/macro/get-macro-summary", macro)
            _store_macro(conn, macro)
            interfaces["macro"] = {"status": "ok" if macro else "fail"}
            log.info("macro: %s", "ok" if macro else "fail")
            sched["macro"] = now

        if _due("sentiment", sched, now):
            sentiment = _fetch_sentiment()
            _write_snapshot("sentiment.json", "news/feed/get-social-sentiment", sentiment)
            _store_sentiment(conn, sentiment)
            interfaces["sentiment"] = {"status": "ok" if sentiment else "fail"}
            log.info("sentiment: %s", "ok" if sentiment else "fail")
            sched["sentiment"] = now

        if _due("reserves", sched, now):
            reserves = _fetch_reserves()
            _write_snapshot("reserves.json", "info/platformmetrics/get-exchange-reserves", reserves)
            _store_reserves(conn, reserves)
            interfaces["reserves"] = {"status": "ok" if reserves else "fail"}
            log.info("reserves: %s", "ok" if reserves else "fail")
            sched["reserves"] = now

        # 合约市场结构数据（按品种遍历，5s 循环下低频接口由 _due 拦截）
        # 注意：_due 在循环前统一判断，sched 更新在循环后——避免首品种触发后
        # 更新调度时间导致其余品种被误判为未到点
        stats_due = _due("stats", sched, now)
        liq_due = _due("liquidations", sched, now)
        ob_due = _due("orderbook", sched, now)
        tr_due = _due("trades", sched, now)
        stats_ok, liq_ok, ob_ok, tr_ok = [], [], [], []
        for contract in CONTRACTS:
            if stats_due:
                stats = _fetch_stats(contract)
                _write_snapshot("stats.json", "cex/futures/market/stats", stats)
                _store_stats(conn, stats, contract)
                if stats is not None:
                    stats_ok.append(contract)
                log.info("stats %s: %s", contract, "ok" if stats is not None else "fail")

            if liq_due:
                liq = _fetch_liquidations(contract)
                _write_snapshot("liquidations.json", "cex/futures/market/liquidations", liq)
                n_new = _store_liquidations(conn, liq, contract)
                if liq is not None:
                    liq_ok.append(contract)
                log.info("liquidations %s: %s, 新增 %s 条", contract, "ok" if liq is not None else "fail", n_new)

            if ob_due:
                ob = _fetch_orderbook(contract)
                _write_snapshot("orderbook.json", "cex/futures/market/orderbook", ob)
                _store_orderbook(conn, ob, contract)
                if ob is not None:
                    ob_ok.append(contract)
                log.info("orderbook %s: %s", contract, "ok" if ob is not None else "fail")

            if tr_due:
                tr = _fetch_trades(contract)
                _write_snapshot("trades.json", "cex/futures/market/trades", tr)
                n_new = _store_trades(conn, tr, contract)
                if tr is not None:
                    tr_ok.append(contract)
                log.info("trades %s: %s, 新增 %s 条", contract, "ok" if tr is not None else "fail", n_new)

        if stats_due:
            sched["stats"] = now
            interfaces["stats"] = {"status": "ok" if len(stats_ok) == len(CONTRACTS) else "fail",
                                   "contracts": stats_ok}
        if liq_due:
            sched["liquidations"] = now
            interfaces["liquidations"] = {"status": "ok" if len(liq_ok) == len(CONTRACTS) else "fail",
                                          "contracts": liq_ok}
        if ob_due:
            sched["orderbook"] = now
            interfaces["orderbook"] = {"status": "ok" if len(ob_ok) == len(CONTRACTS) else "fail",
                                       "contracts": ob_ok}
        if tr_due:
            sched["trades"] = now
            interfaces["trades"] = {"status": "ok" if len(tr_ok) == len(CONTRACTS) else "fail",
                                    "contracts": tr_ok}

        # 情报数据（P0）：市场热度榜 + 上币公告，15min 门控
        if _due("rankings", sched, now):
            rk_ok, rk_new = [], 0
            for rtype in RANKING_TYPES:
                rk = _fetch_rankings(rtype)
                _write_snapshot("rankings.json", "info/coin/get-coin-rankings", rk)
                n_new = _store_rankings(conn, rk, rtype)
                if rk is not None:
                    rk_ok.append(rtype)
                rk_new += n_new
                log.info("rankings %s: %s, 新增 %s 条", rtype, "ok" if rk is not None else "fail", n_new)
            sched["rankings"] = now
            interfaces["rankings"] = {
                "status": "ok" if len(rk_ok) == len(RANKING_TYPES) else "fail",
                "types": rk_ok,
                "new": rk_new,
            }

        if _due("announcements", sched, now):
            ann = _fetch_announcements()
            _write_snapshot("announcements.json", "news/feed/get-exchange-announcements", ann)
            n_new = _store_announcements(conn, ann)
            sched["announcements"] = now
            interfaces["announcements"] = {
                "status": "ok" if ann is not None else "fail",
                "count": len(ann.get("items", [])) if isinstance(ann, dict) else 0,
                "new": n_new,
            }
            log.info("announcements: %s, 新增 %s 条", interfaces["announcements"]["status"], n_new)

        # 社区舆情（P1）：search-ugc 多平台社交帖，30min 门控，按品种遍历
        if _due("social", sched, now):
            sp_ok, sp_new = [], 0
            for contract in CONTRACTS:
                query = XPOST_QUERIES.get(contract, contract)
                coin = contract.split("_")[0]
                sp = _fetch_xposts(query)
                _write_snapshot("social.json", "news/feed/search-ugc", sp)
                n_new = _store_xposts(conn, sp, coin)
                if sp is not None:
                    sp_ok.append(contract)
                sp_new += n_new
                log.info("social %s: %s, 新增 %s 条", contract, "ok" if sp is not None else "fail", n_new)
            sched["social"] = now
            interfaces["social"] = {
                "status": "ok" if len(sp_ok) == len(CONTRACTS) else "fail",
                "contracts": sp_ok,
                "new": sp_new,
            }

        # 币种基本面（P1）：info coin get-coin-info，6h 门控。
        # 仅加密合约（BTC/ETH/SOL）——XAU/XAG 贵金属无有效币种信息（见 COIN_INFO_SYMBOLS 注释）
        if _due("coin_info", sched, now):
            ci_ok, ci_new = [], 0
            for symbol in COIN_INFO_SYMBOLS:
                ci = _fetch_coin_info(symbol)
                _write_snapshot("coin_info.json", "info/coin/get-coin-info", ci)
                n_new = _store_coin_info(conn, ci, symbol)
                if ci is not None:
                    ci_ok.append(symbol)
                ci_new += n_new
                log.info("coin_info %s: %s, 新增 %s 条", symbol, "ok" if ci is not None else "fail", n_new)
            sched["coin_info"] = now
            interfaces["coin_info"] = {
                "status": "ok" if len(ci_ok) == len(COIN_INFO_SYMBOLS) else "fail",
                "symbols": ci_ok,
                "new": ci_new,
            }

        # 技术面情报（P1）：info markettrend get-technical-analysis，30min 门控，按合约遍历。
        # 边界：RSI/MACD 等属非价格行为机制，仅旁路展示，严禁进入节点2 知识库或节点3 执行判断
        if _due("tech_analysis", sched, now):
            ta_ok, ta_new = [], 0
            for contract in CONTRACTS:
                ta = _fetch_tech_analysis(contract)
                _write_snapshot("tech_analysis.json", "info/markettrend/get-technical-analysis", ta)
                n_new = _store_tech_analysis(conn, ta, contract)
                if ta is not None:
                    ta_ok.append(contract)
                ta_new += n_new
                log.info("tech_analysis %s: %s, 新增 %s 条", contract, "ok" if ta is not None else "fail", n_new)
            sched["tech_analysis"] = now
            interfaces["tech_analysis"] = {
                "status": "ok" if len(ta_ok) == len(CONTRACTS) else "fail",
                "contracts": ta_ok,
                "new": ta_new,
            }

        # 链上数据（P2）：info onchain get-token-onchain，6h 门控。
        # 仅 ETH——BTC/SOL 实测返回与 ETH 相同的链上总览（token 参数对原生币不生效），
        # 采集会误导，故不采集（见 ONCHAIN_TOKENS 注释）
        if _due("onchain", sched, now):
            oc_ok, oc_new = [], 0
            for token in ONCHAIN_TOKENS:
                oc = _fetch_onchain(token)
                _write_snapshot("onchain.json", "info/onchain/get-token-onchain", oc)
                n_new = _store_onchain(conn, oc, token)
                if oc is not None:
                    oc_ok.append(token)
                oc_new += n_new
                log.info("onchain %s: %s, 新增 %s 条", token, "ok" if oc is not None else "fail", n_new)
            sched["onchain"] = now
            interfaces["onchain"] = {
                "status": "ok" if len(oc_ok) == len(ONCHAIN_TOKENS) else "fail",
                "tokens": oc_ok,
                "new": oc_new,
            }

        # 事件信号（P2）：news prediction search-events + get-event-signal，30min 门控。
        # 预测类数据仅旁路展示，谨慎呈现（见 PLAN-intel-feed.md §6.2）
        if _due("event_signals", sched, now):
            es = _fetch_event_signals()
            _write_snapshot("event_signals.json", "news/prediction/get-event-signal", es)
            n_new = _store_event_signals(conn, es)
            sched["event_signals"] = now
            interfaces["event_signals"] = {
                "status": "ok" if es is not None else "fail",
                "count": len(es) if isinstance(es, list) else 0,
                "new": n_new,
            }
            log.info("event_signals: %s, 新增 %s 条", interfaces["event_signals"]["status"], n_new)
    finally:
        if now - sched.get("_maint", 0) >= MAINT_INTERVAL:
            deleted = purge_old(conn)
            sched["_maint"] = now
        else:
            deleted = {}
        db_tables = _collect_db_tables(conn)
        conn.close()
        _save_seen(seen)
        _save_schedule(sched)
        _write_status(interfaces, deleted, db_tables)


def main():
    setup_logger()
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        log.error("aux-info-feed 已在运行, 退出")
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--loop":
            log.info("进入循环模式, 间隔 %s 秒", LOOP_INTERVAL)
            while running:
                run_once()
                for _ in range(LOOP_INTERVAL):
                    if not running:
                        break
                    time.sleep(1)
        else:
            run_once()
    finally:
        try:
            lock_handle.close()
            os.remove(LOCK_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    main()