import sqlite3
import json
import os
import sys
import time
import datetime
import signal
import argparse
import threading
import hashlib
import hmac
import urllib.request
import urllib.parse
import urllib.error
import yaml
import websocket

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from logger import setup_logger, logger
from data_monitor import update_data_time, check_and_alert, get_monitor_status
from backup_source import check_and_switch_source, is_backup_active, restore_primary_source, get_backup_status
from health_check import start_health_check, get_health_checker
WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "watchlist.yaml")
KLINE_LOCK_FILE = os.path.join(SCRIPT_DIR, "kline_watcher.lock")
HEALTH_PORT = 18080

# 环境：live=实盘 | testnet=模拟盘（由 --env / PA_DATA_SOURCE_ENV 解析，main() 设置）
CURRENT_ENV = "live"
# 测试网 REST 轮询主采集兜底间隔（秒）：测试网 WS 当前不可达，REST 保证数据持续新鲜
# 2026-08-29 实测仍 10054，60s 更新过慢 → 降至 10s（30 次REST/周期 ≈3req/s，远低于限频）
TESTNET_POLL_INTERVAL = 10


def _pid_alive(pid):
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
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
    """单实例锁：已有存活实例时返回 None（孤儿锁自动接管）"""
    if os.path.exists(KLINE_LOCK_FILE):
        try:
            old = int(open(KLINE_LOCK_FILE, encoding="utf-8").read().strip())
            if old > 0 and _pid_alive(old):
                return None, old
        except Exception:
            pass
        try:
            os.remove(KLINE_LOCK_FILE)
        except Exception:
            pass
    try:
        h = open(KLINE_LOCK_FILE, "x", encoding="utf-8")
        h.write(str(os.getpid()))
        h.flush()
        return h, None
    except FileExistsError:
        try:
            old = int(open(KLINE_LOCK_FILE, encoding="utf-8").read().strip())
        except Exception:
            old = None
        return None, old


DATA_DIR = os.path.join(SCRIPT_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "kline.db")
ACCOUNT_DB_PATH = os.path.join(DATA_DIR, "account.db")

SYMBOL_MAP = {
    "BTC": "BTC_USDT",
    "ETH": "ETH_USDT",
    "SOL": "SOL_USDT",
    "XAU": "XAU_USDT",
    "XAG": "XAG_USDT",
    "AU": "XAU_USDT",
    "AG": "XAG_USDT",
}

running = True
db_conn = None
db_lock = threading.Lock()
account_db_conn = None
account_db_lock = threading.Lock()
watchlist_config = {}
ws_app = None
status_counters = {}
status_lock = threading.Lock()
# 覆盖摘要（flush_status）是**状态**不是事件：原来每次 K 线 upsert 都打一遍
# （`handle_candle_update` → `flush_status()`），而每个轮询周期要 upsert
# 5 品种 × 6 周期 = 30 根。实测占日志 **96%**：199MB / 6 小时 ≈ **541 MB/天**，
# 而 `TimedRotatingFileHandler(backupCount=7)` 按天轮转 ⇒ 峰值可达数 GB。
# 改为「真有变化才打；无变化时最多每 STATUS_HEARTBEAT_SEC 打一次」——
# 保留保活信号（否则「无输出」又会被当成「没在跑」），砍掉绝大部分重复。
STATUS_HEARTBEAT_SEC = 600.0
_last_status_log = 0.0
latest_prices = {}
prices_lock = threading.Lock()
last_disconnect_time = 0
downtime_gaps = {}

# ── 账户推送状态 ───────────────────────────────────────────
account_lock  = threading.Lock()
account_state = {
    "positions":      [],     # 持仓列表 (from futures.positions)
    "balances":       {},     # 余额信息 (from futures.balances)
    "pending_orders": [],     # 未成交挂单 (from futures.orders)
    "auto_orders":    [],     # 计划委托/止盈止损单 (from futures.autoorders)
    "user_trades":    [],     # 成交记录 (from futures.usertrades)
    "push_started_ts": 0,     # 推送线程启动时刻（健康检查宽限期基准）
    "last_success_ts": 0,     # 最近一次成功采集时刻（0=从未成功）
}

ALL_INTERVALS_ORDER = ["5m", "15m", "1h", "4h"]
INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
REST_RECONCILE_INTERVAL_SEC = 300
REST_RECONCILE_SETTLE_DELAY_SEC = 60
REST_RECONCILE_LIMIT = 10

SPOT_WS_URL = "wss://api.gateio.ws/ws/v4/"
FUTURES_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
SPOT_REST_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"
FUTURES_REST_URL = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
GATE_REST = "https://api.gateio.ws"
SPOT_CHANNEL = "spot.candlesticks"
FUTURES_CHANNEL = "futures.candlesticks"
SPOT_TICKER = "spot.tickers"
FUTURES_TICKER = "futures.tickers"

market_type = "spot"


def resolve_symbol(name):
    upper = name.upper()
    if upper in SYMBOL_MAP:
        return SYMBOL_MAP[upper]
    if "_USDT" in upper:
        base = upper.split("_")[0]
        if base in SYMBOL_MAP:
            return SYMBOL_MAP[base]
        return upper
    if not upper.endswith("_USDT"):
        return upper + "_USDT"
    return upper


def format_decimal(value, min_decimals=2):
    try:
        f = float(value)
        s = str(value)
        if "." in s:
            existing = len(s.split(".")[1])
            dec = max(existing, min_decimals)
        else:
            dec = min_decimals
        return f"{f:.{dec}f}"
    except (ValueError, TypeError):
        return str(value)


def get_market_type():
    return watchlist_config.get("market_type", "spot")


def get_ws_url():
    if get_market_type() == "futures":
        return FUTURES_WS_URL
    return SPOT_WS_URL


def get_channel():
    if get_market_type() == "futures":
        return FUTURES_CHANNEL
    return SPOT_CHANNEL


def get_ticker_channel():
    if get_market_type() == "futures":
        return FUTURES_TICKER
    return SPOT_TICKER


def load_config():
    global watchlist_config
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        watchlist_config = yaml.safe_load(f)
    symbols_raw = watchlist_config.get("symbols", [])
    resolved = []
    for entry in symbols_raw:
        if isinstance(entry, str):
            name = resolve_symbol(entry)
            resolved.append({"name": name, "intervals": watchlist_config.get("intervals", ["5m", "15m", "1h", "4h"])})
        elif isinstance(entry, dict):
            name = resolve_symbol(entry["name"])
            intervals = entry.get("intervals", ["5m", "15m", "1h", "4h"])
            resolved.append({"name": name, "intervals": intervals})
    watchlist_config["_resolved_symbols"] = resolved
    # 账户推送配置
    # 密钥优先取环境变量（可移植，不落盘），其次 watchlist.yaml；
    # 测试网例外：GATE_API_KEY 语义为实盘密钥，被继承时会顶掉
    # watchlist_testnet.yaml 内嵌的测试网密钥，导致推送认证失败
    if CURRENT_ENV == "testnet":
        # 优先 GATE_TESTNET_API_KEY/SECRET 环境变量（密钥不落盘），其次 yaml
        watchlist_config["api_key"]          = str(os.environ.get("GATE_TESTNET_API_KEY", watchlist_config.get("api_key", "") or "")).strip()
        watchlist_config["api_secret"]       = str(os.environ.get("GATE_TESTNET_API_SECRET", watchlist_config.get("api_secret", "") or "")).strip()
    else:
        watchlist_config["api_key"]          = str(os.environ.get("GATE_API_KEY", watchlist_config.get("api_key", "") or "")).strip()
        watchlist_config["api_secret"]       = str(os.environ.get("GATE_API_SECRET", watchlist_config.get("api_secret", "") or "")).strip()
    watchlist_config["account_push"]     = bool(watchlist_config.get("account_push", False))
    # 配置意图存档（密钥缺失被强制关闭前的原值）——健康检查用：
    # configured=true 且生效 false = 密钥环境丢失的静默停更签名（08-26 事故）
    watchlist_config["_account_push_configured"] = watchlist_config["account_push"]
    # 未配置密钥时自动关闭账户推送（行情数据公开，账户功能不可用）
    if not watchlist_config["api_key"] or not watchlist_config["api_secret"]:
        watchlist_config["account_push"] = False
    watchlist_config["account_push_interval"] = max(int(watchlist_config.get("account_push_interval", 60) or 60), 10)
    # 环境由 --env / PA_DATA_SOURCE_ENV 解析（main() 已设置 CURRENT_ENV）
    watchlist_config["_env"] = CURRENT_ENV
    # 测试网品种存在性由 validate_contracts() 校验（不再硬编码排除 XAU/XAG）
    _apply_env_urls()
    return resolved


def _apply_env_urls():
    """按 CURRENT_ENV 选择实盘/测试网接口（可移植：默认实盘 live）。"""
    global FUTURES_WS_URL, SPOT_WS_URL, SPOT_REST_URL, FUTURES_REST_URL, GATE_REST
    if CURRENT_ENV == "testnet":
        # 官方新地址（旧地址 fx-ws-testnet.gateio.ws 已 502 废弃）
        FUTURES_WS_URL = "wss://ws-testnet.gate.com/v4/ws/futures/usdt"
        SPOT_WS_URL = "wss://api-testnet.gateapi.io/ws/v4/"
        SPOT_REST_URL = "https://api-testnet.gateapi.io/api/v4/spot/candlesticks"
        FUTURES_REST_URL = "https://api-testnet.gateapi.io/api/v4/futures/usdt/candlesticks"
        GATE_REST = "https://api-testnet.gateapi.io"
    else:
        FUTURES_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
        SPOT_WS_URL = "wss://api.gateio.ws/ws/v4/"
        SPOT_REST_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"
        FUTURES_REST_URL = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
        GATE_REST = "https://api.gateio.ws"


def get_symbol_intervals():
    return watchlist_config.get("_resolved_symbols", [])


def fetch_contracts():
    """查询当前环境合约列表（REST /futures/usdt/contracts，无需密钥）。
    返回合约名集合；查询失败返回 None（跳过校验，不阻塞采集）。"""
    if get_market_type() != "futures":
        return None
    url = f"{GATE_REST}/api/v4/futures/usdt/contracts"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):
            return {c.get("name") for c in data if isinstance(c, dict)}
    except Exception as e:
        logger.warning("合约列表查询失败（跳过存在性校验）: %s", e)
    return None


def validate_contracts():
    """校验 watchlist 品种在当前环境是否存在；不存在的标记 unavailable 并跳过采集，
    不阻塞其他品种。用户可自由增删品种，程序自适应。"""
    contracts = fetch_contracts()
    if contracts is None:
        return
    resolved = get_symbol_intervals()
    available, unavailable = [], []
    for entry in resolved:
        if entry["name"] in contracts:
            available.append(entry)
        else:
            unavailable.append({"name": entry["name"], "reason": "当前环境无此合约"})
            logger.warning("品种 %s 在当前环境（%s）无此合约，跳过采集", entry["name"], CURRENT_ENV)
    watchlist_config["_resolved_symbols"] = available
    watchlist_config["_unavailable_symbols"] = unavailable
    if unavailable:
        logger.info("合约存在性校验: %d 个品种可用, %d 个不可用（%s）",
                    len(available), len(unavailable),
                    ", ".join(u["name"] for u in unavailable))


def get_data_dir():
    """返回数据目录路径（仅用于 SQLite 数据库）"""
    od = watchlist_config.get("output_dir", "./data")
    if od.startswith("./") or od.startswith(".\\"):
        od = os.path.join(SCRIPT_DIR, od[2:])
    elif not os.path.isabs(od):
        od = os.path.join(SCRIPT_DIR, od)
    return od


def get_retention_days():
    return watchlist_config.get("retention_days", 30)


def get_max_candles():
    return watchlist_config.get("max_candles", 500)


def get_interval_seconds(interval):
    return INTERVAL_SECONDS.get(interval, 300)


# ── Gate.io APIv4 签名与 REST 请求 ──────────────────────────
def gate_sign(channel, event, timestamp, secret):
    """WebSocket 鉴权签名: HMAC-SHA512"""
    message = f"channel={channel}&event={event}&time={timestamp}"
    return hmac.new(secret.encode("utf8"), message.encode("utf8"), hashlib.sha512).hexdigest()

def rest_signed_request(method, path, query_string="", api_key="", api_secret=""):
    """Gate APIv4 REST 签名请求"""
    url = f"{GATE_REST}{path}"
    if query_string:
        url += f"?{query_string}"
    body = ""
    body_hash = hashlib.sha512(body.encode("utf8")).hexdigest()
    timestamp = str(int(time.time()))
    sign_str = f"{method}\n{path}\n{query_string}\n{body_hash}\n{timestamp}"
    sign = hmac.new(api_secret.encode("utf8"), sign_str.encode("utf8"), hashlib.sha512).hexdigest()
    headers = {
        "KEY": api_key,
        "SIGN": sign,
        "Timestamp": timestamp,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "kline-watcher/1.0",
    }
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf8"))
    except Exception as e:
        logger.error("REST 请求失败 %s: %s", path, e)
        return None


def init_db():
    global db_conn
    os.makedirs(DATA_DIR, exist_ok=True)
    db_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    db_conn.execute("PRAGMA journal_mode=WAL")
    db_conn.execute("PRAGMA synchronous=NORMAL")
    db_conn.execute("""
        CREATE TABLE IF NOT EXISTS [kline] (
            t INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            o TEXT NOT NULL, h TEXT NOT NULL, l TEXT NOT NULL, c TEXT NOT NULL,
            v TEXT NOT NULL, sum TEXT NOT NULL,
            ema20 TEXT, atr14 TEXT,
            PRIMARY KEY (symbol, interval, t)
        )
    """)
    db_conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_time ON [kline] (t)")
    db_conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_symbol ON [kline] (symbol, t)")
    db_conn.commit()


def init_account_db():
    """创建独立的账户历史数据库 (account.db)：balance/position/order/price_order/trade 各含 current+history"""
    global account_db_conn
    os.makedirs(DATA_DIR, exist_ok=True)
    account_db_conn = sqlite3.connect(ACCOUNT_DB_PATH, check_same_thread=False, timeout=30)
    account_db_conn.execute("PRAGMA journal_mode=WAL")
    account_db_conn.execute("PRAGMA synchronous=NORMAL")
    # 余额历史
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS balance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            total TEXT,
            available TEXT,
            unrealised_pnl TEXT,
            position_margin TEXT,
            order_margin TEXT
        )
    """)
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_balance_ts ON balance_history (timestamp)")
    # 持仓历史
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS position_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            contract TEXT NOT NULL,
            mode TEXT,
            size TEXT,
            entry_price TEXT,
            mark_price TEXT,
            unrealised_pnl TEXT,
            leverage TEXT,
            liq_price TEXT,
            margin TEXT
        )
    """)
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_position_ts ON position_history (timestamp)")
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_position_contract ON position_history (contract, timestamp)")
    # 持仓当前状态（按品种聚合，方便 AI 查询）
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS position_current (
            contract TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL,
            updated_at_str TEXT,
            updated_at_local_str TEXT,
            size TEXT,
            entry_price TEXT,
            mark_price TEXT,
            unrealised_pnl TEXT,
            leverage TEXT,
            liq_price TEXT,
            margin TEXT,
            PRIMARY KEY (contract, mode)
        )
    """)
    # 挂单历史
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS order_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            order_id TEXT NOT NULL,
            contract TEXT NOT NULL,
            size TEXT,
            price TEXT,
            left TEXT,
            status TEXT,
            event TEXT
        )
    """)
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_order_ts ON order_history (timestamp)")
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_order_id ON order_history (order_id)")
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_order_contract_ts ON order_history (contract, timestamp)")
    # 成交历史
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            trade_id TEXT,
            contract TEXT NOT NULL,
            size TEXT,
            price TEXT,
            role TEXT,
            realised_pnl TEXT,
            fee TEXT
        )
    """)
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_ts ON trade_history (timestamp)")
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_contract_ts ON trade_history (contract, timestamp)")
    # 历史遗留：idx_trade_contract 与 idx_trade_contract_ts 完全重复，清理掉
    account_db_conn.execute("DROP INDEX IF EXISTS idx_trade_contract")
    # 挂单当前状态（按品种聚合，方便 AI 查询）
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS order_current (
            order_id TEXT PRIMARY KEY,
            updated_at INTEGER NOT NULL,
            updated_at_str TEXT,
            updated_at_local_str TEXT,
            contract TEXT NOT NULL,
            size TEXT,
            price TEXT,
            left TEXT,
            status TEXT
        )
    """)
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_order_current_contract ON order_current (contract)")
    # 计划委托/止盈止损当前状态（order_current 仅存普通挂单，触发单字段结构不同，独立建表）
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS price_order_current (
            order_id TEXT PRIMARY KEY,
            updated_at INTEGER NOT NULL,
            updated_at_str TEXT,
            updated_at_local_str TEXT,
            contract TEXT NOT NULL,
            size TEXT,
            price TEXT,
            trigger_price TEXT,
            rule TEXT,
            is_stop INTEGER,
            status TEXT
        )
    """)
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_price_order_current_contract ON price_order_current (contract)")
    # 计划委托/止盈止损事件历史
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS price_order_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            order_id TEXT NOT NULL,
            contract TEXT NOT NULL,
            size TEXT,
            price TEXT,
            trigger_price TEXT,
            rule TEXT,
            is_stop INTEGER,
            status TEXT,
            event TEXT
        )
    """)
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_price_order_ts ON price_order_history (timestamp)")
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_price_order_id ON price_order_history (order_id)")
    account_db_conn.execute("CREATE INDEX IF NOT EXISTS idx_price_order_contract_ts ON price_order_history (contract, timestamp)")
    # 余额当前状态（单条记录，方便 AI 查询）
    account_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS balance_current (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at INTEGER NOT NULL,
            updated_at_str TEXT,
            updated_at_local_str TEXT,
            total TEXT,
            available TEXT,
            unrealised_pnl TEXT,
            position_margin TEXT,
            order_margin TEXT
        )
    """)
    account_db_conn.commit()


def get_existing_timestamps(symbol, interval):
    with db_lock:
        cur = db_conn.execute("SELECT t FROM [kline] WHERE symbol=? AND interval=?", (symbol, interval))
        return {row[0] for row in cur.fetchall()}


def list_recent(symbol, interval, limit):
    with db_lock:
        cur = db_conn.execute(
            "SELECT t,o,h,l,c,v,sum,ema20,atr14 FROM [kline] WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?",
            (symbol, interval, limit),
        )
        rows = cur.fetchall()
    return [
        {"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5], "sum": r[6], "ema20": r[7], "atr14": r[8]}
        for r in reversed(rows)
    ]


def list_all_sorted(symbol, interval):
    with db_lock:
        cur = db_conn.execute(
            "SELECT t,o,h,l,c,v,sum,ema20,atr14 FROM [kline] WHERE symbol=? AND interval=? ORDER BY t ASC",
            (symbol, interval),
        )
        rows = cur.fetchall()
    return [
        {"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5], "sum": r[6], "ema20": r[7], "atr14": r[8]}
        for r in rows
    ]


def upsert_candle(symbol, interval, candle):
    with db_lock:
        db_conn.execute(
            "INSERT OR REPLACE INTO [kline] (symbol,interval,t,o,h,l,c,v,sum,ema20,atr14) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                symbol, interval, candle["t"],
                str(candle["o"]), str(candle["h"]), str(candle["l"]), str(candle["c"]),
                str(candle["v"]), str(candle["sum"]),
                candle.get("ema20"), candle.get("atr14"),
            ),
        )
        db_conn.commit()


def _truncate_max_candles(symbol, interval):
    """行数超过 max_candles 时仅保留最新 max_candles 根（<=0 表示不限制）。
    必须在已持有 db_lock 的上下文中调用。"""
    max_candles = get_max_candles()
    if max_candles <= 0:
        return
    count = db_conn.execute(
        "SELECT COUNT(*) FROM [kline] WHERE symbol=? AND interval=?",
        (symbol, interval),
    ).fetchone()[0]
    if count <= max_candles:
        return
    db_conn.execute(
        """DELETE FROM [kline] WHERE symbol=? AND interval=? AND t < (
               SELECT MIN(t) FROM (
                   SELECT t FROM [kline] WHERE symbol=? AND interval=?
                   ORDER BY t DESC LIMIT ?
               )
           )""",
        (symbol, interval, symbol, interval, max_candles),
    )


def retention_cleanup(symbol=None, interval=None):
    """清理过期数据并按 max_candles 截断。
    定向模式（symbol+interval）返回该序列当前行数，供状态统计复用，避免重复 COUNT。"""
    retention_days = get_retention_days()
    cutoff = int(time.time()) - retention_days * 86400 if retention_days > 0 else None
    if symbol and interval:
        with db_lock:
            if cutoff is not None:
                db_conn.execute("DELETE FROM [kline] WHERE symbol=? AND interval=? AND t < ?", (symbol, interval, cutoff))
            _truncate_max_candles(symbol, interval)
            total = db_conn.execute(
                "SELECT COUNT(*) FROM [kline] WHERE symbol=? AND interval=?",
                (symbol, interval),
            ).fetchone()[0]
            db_conn.commit()
            return total
    else:
        with db_lock:
            for entry in get_symbol_intervals():
                for iv in entry["intervals"]:
                    if cutoff is not None:
                        db_conn.execute("DELETE FROM [kline] WHERE symbol=? AND interval=? AND t < ?", (entry["name"], iv, cutoff))
                    _truncate_max_candles(entry["name"], iv)
            db_conn.commit()
        return None


ACCOUNT_RETENTION_DAYS = 90


def cleanup_account_data():
    """清理 account.db 中超过90天的历史数据"""
    if not account_db_conn:
        return
    cutoff = int(time.time()) - ACCOUNT_RETENTION_DAYS * 86400
    total_deleted = 0
    try:
        with account_db_lock:
            for table in ("balance_history", "position_history", "order_history", "trade_history"):
                cur = account_db_conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                total_deleted += cur.rowcount
            account_db_conn.commit()
        if total_deleted > 0:
            logger.info("account.db 清理完成: 删除 %d 条超过 %d 天的记录", total_deleted, ACCOUNT_RETENTION_DAYS)
        else:
            logger.info("account.db 无需清理 (保留 %d 天)", ACCOUNT_RETENTION_DAYS)
    except Exception as e:
        logger.error("account.db 清理失败: %s", e)


def fetch_historical(symbol, interval, limit):
    if get_market_type() == "futures":
        url = f"{FUTURES_REST_URL}?contract={symbol}&limit={limit}&interval={interval}"
    else:
        url = f"{SPOT_REST_URL}?currency_pair={symbol}&limit={limit}&interval={interval}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                break
        except (urllib.error.URLError, Exception) as e:
            if attempt < 2:
                time.sleep(2)
                continue
            logger.error("REST error fetching %s %s: %s", symbol, interval, e)
            return []
    result = []
    if get_market_type() == "futures":
        for row in data:
            result.append({
                "t": int(row["t"]),
                "v": format_decimal(row["v"]),
                "c": format_decimal(row["c"]),
                "h": format_decimal(row["h"]),
                "l": format_decimal(row["l"]),
                "o": format_decimal(row["o"]),
                "sum": format_decimal(row.get("sum", "0")),
            })
    else:
        for row in data:
            result.append({
                "t": int(row[0]),
                "v": format_decimal(row[1]),
                "c": format_decimal(row[2]),
                "h": format_decimal(row[3]),
                "l": format_decimal(row[4]),
                "o": format_decimal(row[5]),
                "sum": format_decimal(row[6]),
            })
    return result


def compute_ema(data, period=20):
    if len(data) < period:
        return
    k = 2.0 / (period + 1)
    sma = sum(float(d["c"]) for d in data[:period]) / period
    for i in range(period - 1):
        data[i]["ema20"] = None
    data[period - 1]["ema20"] = str(round(sma, 8))
    for i in range(period, len(data)):
        prev_ema = float(data[i - 1]["ema20"])
        close_val = float(data[i]["c"])
        ema_val = close_val * k + prev_ema * (1 - k)
        data[i]["ema20"] = str(round(ema_val, 8))


def compute_atr(data, period=14):
    if len(data) < 2:
        for d in data:
            d["atr14"] = None
        return
    data[0]["atr14"] = None
    trs = []
    for i in range(1, len(data)):
        h = float(data[i]["h"])
        l = float(data[i]["l"])
        pc = float(data[i - 1]["c"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        if i < period:
            data[i]["atr14"] = None
        elif i == period:
            atr_val = sum(trs) / period
            data[i]["atr14"] = str(round(atr_val, 8))
        else:
            prev_atr = float(data[i - 1]["atr14"])
            atr_val = (prev_atr * (period - 1) + tr) / period
            data[i]["atr14"] = str(round(atr_val, 8))


def compute_latest_atr(symbol, interval, new_h, new_l, new_c, prev_c, new_t):
    with db_lock:
        cur = db_conn.execute(
            "SELECT t,h,l,c,atr14 FROM [kline] WHERE symbol=? AND interval=? AND atr14 IS NOT NULL AND t < ? ORDER BY t DESC LIMIT 1",
            (symbol, interval, new_t),
        )
        row = cur.fetchone()
    if row is None:
        return None
    tr = max(float(new_h) - float(new_l), abs(float(new_h) - float(prev_c)), abs(float(new_l) - float(prev_c)))
    atr_val = (float(row[4]) * 13 + tr) / 14
    return str(round(atr_val, 8))


def recalculate_all_ema(symbol, interval):
    data = list_all_sorted(symbol, interval)
    if not data:
        return
    compute_ema(data)
    compute_atr(data)
    with db_lock:
        for d in data:
            db_conn.execute(
                "UPDATE [kline] SET ema20=?, atr14=? WHERE symbol=? AND interval=? AND t=?",
                (d.get("ema20"), d.get("atr14"), symbol, interval, d["t"]),
            )
        db_conn.commit()


def reconcile_recent_completed_candles(limit=REST_RECONCILE_LIMIT, settle_delay_sec=REST_RECONCILE_SETTLE_DELAY_SEC):
    """Use REST as the source of truth for recently completed candles."""
    if not db_conn:
        return

    current_time = int(time.time())
    total_checked = 0
    total_updated = 0

    for entry in get_symbol_intervals():
        symbol = entry["name"]
        for interval in entry["intervals"]:
            interval_seconds = get_interval_seconds(interval)
            try:
                historical = fetch_historical(symbol, interval, limit)
                if not historical:
                    continue

                interval_updates = 0
                for candle in historical:
                    candle_end = candle["t"] + interval_seconds
                    if candle_end > current_time - settle_delay_sec:
                        continue

                    total_checked += 1
                    with db_lock:
                        row = db_conn.execute(
                            "SELECT o,h,l,c,v,sum FROM [kline] WHERE symbol=? AND interval=? AND t=?",
                            (symbol, interval, candle["t"]),
                        ).fetchone()

                    fields = ["o", "h", "l", "c", "v", "sum"]
                    needs_update = row is None or any(str(row[i]) != str(candle[fields[i]]) for i in range(len(fields)))
                    if needs_update:
                        upsert_candle(symbol, interval, candle)
                        interval_updates += 1

                if interval_updates:
                    recalculate_all_ema(symbol, interval)
                    total_updated += interval_updates
                    logger.info("REST校准 %s %s: 更新 %d 根已收盘K线", symbol, interval, interval_updates)
            except Exception as e:
                logger.error("REST校准异常 %s %s: %s", symbol, interval, e)
            time.sleep(0.1)

    if total_updated:
        logger.info("REST校准汇总: 检查 %d 根, 更新 %d 根", total_checked, total_updated)


def compute_latest_ema(symbol, interval, new_c, new_t):
    with db_lock:
        cur = db_conn.execute(
            "SELECT t,c,ema20 FROM [kline] WHERE symbol=? AND interval=? AND ema20 IS NOT NULL AND t < ? ORDER BY t DESC LIMIT 1",
            (symbol, interval, new_t),
        )
        row = cur.fetchone()
    if row is None:
        return None
    k = 2.0 / 21.0
    ema_val = float(new_c) * k + float(row[2]) * (1 - k)
    return str(round(ema_val, 8))


# ── 账户数据持久化 ────────────────────────────────────────
def save_balance_history(balances):
    """将余额快照写入 account.db 的 balance_history 表，同时更新 balance_current"""
    if not balances:
        return
    now = int(time.time())
    try:
        with account_db_lock:
            # 写入历史
            account_db_conn.execute(
                "INSERT INTO balance_history (timestamp, total, available, unrealised_pnl, position_margin, order_margin) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now,
                    str(balances.get("total", "")),
                    str(balances.get("available", "")),
                    str(balances.get("unrealised_pnl", "")),
                    str(balances.get("position_margin", "")),
                    str(balances.get("order_margin", "")),
                ),
            )
            # 更新当前状态（UPSERT，强制 id=1）
            now_str = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            now_local_str = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
            account_db_conn.execute(
                "INSERT OR REPLACE INTO balance_current (id, updated_at, updated_at_str, updated_at_local_str, total, available, unrealised_pnl, position_margin, order_margin) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    now_str,
                    now_local_str,
                    str(balances.get("total", "")),
                    str(balances.get("available", "")),
                    str(balances.get("unrealised_pnl", "")),
                    str(balances.get("position_margin", "")),
                    str(balances.get("order_margin", "")),
                ),
            )
            account_db_conn.commit()
    except Exception as e:
        logger.error("save_balance_history 失败: %s", e)


def save_position_history(positions):
    """将持仓快照写入 account.db 的 position_history 表，同时更新 position_current"""
    if not isinstance(positions, list):
        return
    now = int(time.time())
    try:
        with account_db_lock:
            # 写入历史
            for pos in positions:
                account_db_conn.execute(
                    "INSERT INTO position_history "
                    "(timestamp, contract, mode, size, entry_price, mark_price, unrealised_pnl, leverage, liq_price, margin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        str(pos.get("contract", "")),
                        str(pos.get("mode", "")),
                        str(pos.get("size", "0")),
                        str(pos.get("entry_price", "0")),
                        str(pos.get("mark_price", "0")),
                        str(pos.get("unrealised_pnl", "0")),
                        str(pos.get("leverage", "0")),
                        str(pos.get("liq_price", "0")),
                        str(pos.get("margin", "0")),
                    ),
                )
            # 更新当前状态（UPSERT）
            now_str = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            now_local_str = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
            for pos in positions:
                contract = str(pos.get("contract", ""))
                account_db_conn.execute(
                    "INSERT OR REPLACE INTO position_current "
                    "(contract, updated_at, updated_at_str, updated_at_local_str, mode, size, entry_price, mark_price, unrealised_pnl, leverage, liq_price, margin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        contract,
                        now,
                        now_str,
                        now_local_str,
                        str(pos.get("mode", "")),
                        str(pos.get("size", "0")),
                        str(pos.get("entry_price", "0")),
                        str(pos.get("mark_price", "0")),
                        str(pos.get("unrealised_pnl", "0")),
                        str(pos.get("leverage", "0")),
                        str(pos.get("liq_price", "0")),
                        str(pos.get("margin", "0")),
                    ),
                )
            account_db_conn.commit()
    except Exception as e:
        logger.error("save_position_history 失败: %s", e)


_order_status_cache = {}  # order_id -> 上次状态；轮询去重用，history 只记真实事件与状态变化


def save_order_history(order, event):
    """将挂单事件写入 account.db 的 order_history 表，同时更新 order_current

    poll/initial 轮询事件仅在状态变化时写 history（避免每 60 秒快照刷屏）；
    WS put/update/finish 事件始终写 history。
    """
    if not order:
        return
    now = int(time.time())
    order_id = str(order.get("id", ""))
    contract = str(order.get("contract", ""))
    status = str(order.get("status", ""))
    try:
        with account_db_lock:
            status_changed = _order_status_cache.get(order_id) != status
            if event not in ("poll", "initial") or status_changed:
                account_db_conn.execute(
                    "INSERT INTO order_history "
                    "(timestamp, order_id, contract, size, price, left, status, event) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        order_id,
                        contract,
                        str(order.get("size", "0")),
                        str(order.get("price", "0")),
                        str(order.get("left", "0")),
                        status,
                        event,
                    ),
                )
            _order_status_cache[order_id] = status
            # 更新当前状态（finished 则删除，否则 UPSERT）
            now_str = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            now_local_str = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
            if status in ("finished", "cancelled"):
                account_db_conn.execute("DELETE FROM order_current WHERE order_id=?", (order_id,))
            else:
                account_db_conn.execute(
                    "INSERT OR REPLACE INTO order_current "
                    "(order_id, updated_at, updated_at_str, updated_at_local_str, contract, size, price, left, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        order_id,
                        now,
                        now_str,
                        now_local_str,
                        contract,
                        str(order.get("size", "0")),
                        str(order.get("price", "0")),
                        str(order.get("left", "0")),
                        status,
                    ),
                )
            account_db_conn.commit()
    except Exception as e:
        logger.error("save_order_history 失败: %s", e)


_price_order_status_cache = {}  # 计划委托 order_id -> 上次状态；轮询去重用


def save_price_order(order, event):
    """将计划委托/止盈止损单写入 account.db（price_order_history + price_order_current）

    与 save_order_history 同一套语义：poll/initial 仅在状态变化时写 history，
    WS put/update/finish 始终写 history；current 表 finished/cancelled/failed 删除、否则 UPSERT。
    REST 响应用 rule 字段，WS 推送用 trigger_rule，两者都兼容。
    """
    if not order:
        return
    now = int(time.time())
    order_id = str(order.get("id", ""))
    contract = str(order.get("contract", ""))
    status = str(order.get("status", ""))
    size = str(order.get("size", "0"))
    price = str(order.get("price", "0"))
    trigger_price = str(order.get("trigger_price", "0"))
    rule = str(order.get("rule", "") or order.get("trigger_rule", ""))
    is_stop = 1 if order.get("is_stop_order") else 0
    try:
        with account_db_lock:
            status_changed = _price_order_status_cache.get(order_id) != status
            if event not in ("poll", "initial") or status_changed:
                account_db_conn.execute(
                    "INSERT INTO price_order_history "
                    "(timestamp, order_id, contract, size, price, trigger_price, rule, is_stop, status, event) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, order_id, contract, size, price, trigger_price, rule, is_stop, status, event),
                )
            _price_order_status_cache[order_id] = status
            now_str = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            now_local_str = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
            if status in ("finished", "cancelled", "failed"):
                account_db_conn.execute("DELETE FROM price_order_current WHERE order_id=?", (order_id,))
            else:
                account_db_conn.execute(
                    "INSERT OR REPLACE INTO price_order_current "
                    "(order_id, updated_at, updated_at_str, updated_at_local_str, contract, size, price, trigger_price, rule, is_stop, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (order_id, now, now_str, now_local_str, contract, size, price, trigger_price, rule, is_stop, status),
                )
            account_db_conn.commit()
    except Exception as e:
        logger.error("save_price_order 失败: %s", e)


def save_trade_history(trades):
    """将成交记录写入 account.db 的 trade_history 表"""
    if not isinstance(trades, list):
        return
    now = int(time.time())
    try:
        with account_db_lock:
            for trade in trades:
                account_db_conn.execute(
                    "INSERT INTO trade_history "
                    "(timestamp, trade_id, contract, size, price, role, realised_pnl, fee) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        str(trade.get("id", "")),
                        str(trade.get("contract", "")),
                        str(trade.get("size", "0")),
                        str(trade.get("price", "0")),
                        str(trade.get("role", "")),
                        str(trade.get("realised_pnl", "0")),
                        str(trade.get("fee", "0")),
                    ),
                )
            account_db_conn.commit()
    except Exception as e:
        logger.error("save_trade_history 失败: %s", e)


# ── REST API 获取账户状态 ─────────────────────────────────
def fetch_account_positions():
    """获取当前持仓列表"""
    api_key = watchlist_config.get("api_key", "")
    api_secret = watchlist_config.get("api_secret", "")
    if not api_key or not api_secret:
        return []
    result = rest_signed_request("GET", "/api/v4/futures/usdt/positions", "", api_key, api_secret)
    if result is None:
        return []
    if isinstance(result, list):
        return [p for p in result if p.get("size", "0") != "0"]
    return []

def fetch_account_balance():
    """获取当前账户余额"""
    api_key = watchlist_config.get("api_key", "")
    api_secret = watchlist_config.get("api_secret", "")
    if not api_key or not api_secret:
        return {}
    result = rest_signed_request("GET", "/api/v4/futures/usdt/accounts", "", api_key, api_secret)
    if result is None:
        return {}
    return result

def fetch_account_orders():
    """获取当前未成交挂单"""
    api_key = watchlist_config.get("api_key", "")
    api_secret = watchlist_config.get("api_secret", "")
    if not api_key or not api_secret:
        return []
    result = rest_signed_request("GET", "/api/v4/futures/usdt/orders", "status=open", api_key, api_secret)
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return []

def fetch_account_autoorders():
    """获取当前计划委托/止盈止损单"""
    api_key = watchlist_config.get("api_key", "")
    api_secret = watchlist_config.get("api_secret", "")
    if not api_key or not api_secret:
        return []
    result = rest_signed_request("GET", "/api/v4/futures/usdt/price_orders", "status=open", api_key, api_secret)
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return []

def initial_account_load():
    """启动时获取初始账户状态"""
    if not watchlist_config.get("account_push"):
        return
    if not watchlist_config.get("api_key") or not watchlist_config.get("api_secret"):
        logger.warning("账户推送: 未配置 api_key/api_secret，跳过")
        return
    logger.info("正在获取初始账户状态...")
    positions = fetch_account_positions()
    balance = fetch_account_balance()
    orders = fetch_account_orders()
    autoorders = fetch_account_autoorders()
    with account_lock:
        account_state["positions"] = positions
        account_state["balances"] = balance
        account_state["pending_orders"] = orders
        account_state["auto_orders"] = autoorders
        account_state["last_success_ts"] = time.time()
    save_balance_history(balance)
    save_position_history(positions)
    for o in orders:
        save_order_history(o, "initial")
    for o in autoorders:
        save_price_order(o, "initial")
    _reconcile_order_current(orders)
    _reconcile_price_order_current(autoorders)
    logger.info("初始账户状态: %d 个持仓, %d 个挂单, %d 个计划委托", len(positions), len(orders), len(autoorders))
    if balance:
        logger.info("账户余额: 总计 %s USDT, 可用 %s USDT", balance.get('total', 'N/A'), balance.get('available', 'N/A'))


# ── 定时账户汇总输出 ──────────────────────────────────────
def _query_account_db(sql, params=()):
    """辅助函数：从 account.db 执行查询，返回所有行"""
    if not account_db_conn:
        return []
    with account_db_lock:
        cur = account_db_conn.execute(sql, params)
        return cur.fetchall()


def print_account_summary():
    """打印账户汇总：余额 + 持仓 + 挂单 + 最近成交(从account.db读取)"""
    with account_lock:
        positions = list(account_state["positions"])
        balances = dict(account_state["balances"])
        pending = list(account_state["pending_orders"])

    logger.info("=" * 60)
    logger.info("=== 账户汇总 ===")

    if balances:
        total_val = balances.get("total", "N/A")
        avail_val = balances.get("available", "N/A")
        unrealised = balances.get("unrealised_pnl", "N/A")
        logger.info("  余额: 总计=%s USDT  可用=%s USDT  浮盈=%s USDT", total_val, avail_val, unrealised)

    active_positions = [p for p in positions if p.get("size", "0") != "0"]
    if active_positions:
        logger.info("  持仓 (%d 个):", len(active_positions))
        for pos in active_positions:
            contract = pos.get("contract", "")
            mode = pos.get("mode", "")
            size = pos.get("size", "0")
            entry = pos.get("entry_price", "0")
            mark = pos.get("mark_price", "0")
            pnl = pos.get("unrealised_pnl", "0")
            lever = pos.get("leverage", "0")
            liq = pos.get("liq_price", "0")
            margin = pos.get("margin", "0")
            logger.info("    %s %s 数量=%s 入场=%s 标记=%s 浮盈=%s 杠杆=%sx 保证金=%s 爆仓价=%s", contract, mode, size, entry, mark, pnl, lever, margin, liq)
    else:
        logger.info("  持仓: 无")

    if pending:
        logger.info("  挂单 (%d 个):", len(pending))
        for order in pending:
            contract = order.get("contract", "")
            size = int(order.get("size", "0"))
            direction = "long" if size > 0 else "short"
            price = order.get("price", "0")
            amount = abs(size)
            left = float(order.get("left", "0"))
            filled_amount = amount - left if left <= amount else 0
            logger.info("    %s %s 价格=%s 数量=%d 已成交=%.0f", contract, direction, price, amount, filled_amount)
    else:
        logger.info("  挂单: 无")

    auto_orders = list(account_state.get("auto_orders", []))
    if auto_orders:
        logger.info("  计划委托 (%d 个):", len(auto_orders))
        for order in auto_orders:
            contract = order.get("contract", "")
            size = int(order.get("size", "0"))
            direction = "long" if size > 0 else "short"
            trigger = order.get("trigger_price", "0")
            price = order.get("price", "0")
            is_stop = order.get("is_stop_order", False)
            label = "止损" if is_stop else "计划"
            logger.info("    [%s] %s %s 触发=%s 委托=%s 数量=%d", label, contract, direction, trigger, price, abs(size))
    else:
        logger.info("  计划委托: 无")

    # ── 从 account.db 读取最近成交记录 ──
    recent_trades = _query_account_db(
        "SELECT timestamp, contract, size, price, role, realised_pnl, fee "
        "FROM trade_history ORDER BY id DESC LIMIT 5"
    )
    if recent_trades:
        logger.info("  最近成交 (account.db):")
        for row in recent_trades:
            ts_str = time.strftime("%m-%d %H:%M:%S", time.localtime(row[0]))
            contract = row[1]
            size = row[2]
            price = row[3]
            role = row[4]
            pnl = row[5]
            fee = row[6]
            direction = "long" if int(size) > 0 else "short"
            logger.info("    [%s] %s %s 价格=%s 数量=%s 角色=%s 盈亏=%s 手续费=%s", ts_str, contract, direction, price, size, role, pnl, fee)

    # ── account.db 统计信息 ──
    db_counts = {}
    for table in ("balance_history", "position_history", "order_history", "price_order_history", "trade_history"):
        rows = _query_account_db(f"SELECT COUNT(*) FROM {table}")
        db_counts[table] = rows[0][0] if rows else 0
    total_records = sum(db_counts.values())
    if total_records > 0:
        logger.info("  DB记录: 余额=%d 持仓=%d 挂单=%d 计划委托=%d 成交=%d (共%d)", db_counts['balance_history'], db_counts['position_history'], db_counts['order_history'], db_counts['price_order_history'], db_counts['trade_history'], total_records)

    logger.info("=" * 60)

def account_push_loop():
    """定时获取账户数据并打印汇总的守护线程"""
    interval = watchlist_config.get("account_push_interval", 60)
    with account_lock:
        account_state["push_started_ts"] = time.time()
    while running:
        for _ in range(interval):
            if not running:
                return
            time.sleep(1)
        if not running:
            break
        try:
            # 主动获取最新账户数据
            positions = fetch_account_positions()
            balance = fetch_account_balance()
            orders = fetch_account_orders()
            autoorders = fetch_account_autoorders()
            with account_lock:
                account_state["positions"] = positions
                account_state["balances"] = balance
                account_state["pending_orders"] = orders
                account_state["auto_orders"] = autoorders
                account_state["last_success_ts"] = time.time()
            # 保存到数据库
            save_balance_history(balance)
            save_position_history(positions)
            for o in orders:
                save_order_history(o, "poll")
            for o in autoorders:
                save_price_order(o, "poll")
            _reconcile_order_current(orders)
            _reconcile_price_order_current(autoorders)
            # 打印汇总
            print_account_summary()
        except Exception as e:
            logger.error("账户推送循环异常: %s", e)


def _reconcile_order_current(orders):
    """校准 order_current：删除已不在挂单列表中的记录（撤单/成交后无 WS 事件时兜底）"""
    try:
        known_ids = {str(o.get("id", "")) for o in orders if o.get("id")}
        with account_db_lock:
            rows = account_db_conn.execute("SELECT order_id FROM order_current").fetchall()
            for (oid,) in rows:
                if str(oid) not in known_ids:
                    account_db_conn.execute("DELETE FROM order_current WHERE order_id=?", (oid,))
            account_db_conn.commit()
    except Exception as e:
        logger.error("order_current 校准失败: %s", e)


def _reconcile_price_order_current(autoorders):
    """校准 price_order_current：删除已不在计划委托列表中的记录（触发/撤销后无 WS 事件时兜底）"""
    try:
        known_ids = {str(o.get("id", "")) for o in autoorders if o.get("id")}
        with account_db_lock:
            rows = account_db_conn.execute("SELECT order_id FROM price_order_current").fetchall()
            for (oid,) in rows:
                if str(oid) not in known_ids:
                    account_db_conn.execute("DELETE FROM price_order_current WHERE order_id=?", (oid,))
            account_db_conn.commit()
    except Exception as e:
        logger.error("price_order_current 校准失败: %s", e)


def timestamp_str():
    return time.strftime("%H:%M:%S", time.localtime())


def update_status(symbol, interval, action, count=None):
    key = f"{symbol}_{interval}"
    with status_lock:
        if key not in status_counters:
            status_counters[key] = {"added": 0, "total": 0}
        if action == "add":
            status_counters[key]["added"] += 1
            if count is not None:
                status_counters[key]["total"] = count
        elif action == "set_total":
            status_counters[key]["total"] = count


def flush_status():
    global _last_status_log
    with status_lock:
        now = time.time()
        # 覆盖摘要是**状态**不是事件 —— 固定低频输出即可（默认 10 分钟一次）。
        #
        # **不能**用「有变化才打」来节流：当前那根未完成的 K 线每个轮询周期都会被
        # upsert，于是 `added` 几乎每轮都 > 0、「变化」几乎总成立 —— 实测那样改
        # 仍然 ~440MB/天，等于没节流（第一版就是这么写错的）。
        #
        # 提前 return 时**不清计数器**，所以 `+N` 会是这一整个窗口的累计增量。
        if (now - _last_status_log) < STATUS_HEARTBEAT_SEC:
            return
        _last_status_log = now
        parts = []
        for entry in get_symbol_intervals():
            for interval in entry["intervals"]:
                key = f"{entry['name']}_{interval}"
                sc = status_counters.get(key, {"added": 0, "total": 0})
                name = entry["name"]
                if sc["added"] > 0:
                    parts.append(f"{name} {interval} +{sc['added']} ({sc['total']})")
                    sc["added"] = 0
                else:
                    parts.append(f"{name} {interval} ({sc['total']})")
        if parts:
            logger.info(" | ".join(parts))


def handle_candle_update(n_field, candle_data):
    parts = n_field.split("_", 1)
    if len(parts) < 2:
        return
    interval = parts[0].lower()
    raw_symbol = parts[1]
    full_symbol = resolve_symbol(raw_symbol)
    tracked = False
    for entry in get_symbol_intervals():
        if entry["name"].upper() == full_symbol.upper() and interval in entry["intervals"]:
            tracked = True
            break
    if not tracked:
        return
    t_val = int(candle_data["t"])
    o_val = format_decimal(candle_data["o"])
    h_val = format_decimal(candle_data["h"])
    l_val = format_decimal(candle_data["l"])
    c_val = format_decimal(candle_data["c"])
    v_val = format_decimal(candle_data.get("v", "0"))
    a_val = format_decimal(candle_data.get("a", "0"))
    candle = {
        "t": t_val,
        "o": o_val,
        "h": h_val,
        "l": l_val,
        "c": c_val,
        "v": str(v_val),
        "sum": str(a_val),
    }

    market_type = get_market_type()
    checked_candle = check_and_switch_source(full_symbol, interval, candle, market_type)
    if checked_candle is not None:
        candle = checked_candle

    ema_val = compute_latest_ema(full_symbol, interval, str(candle["c"]), t_val)
    if ema_val is not None:
        candle["ema20"] = ema_val
    # 查找前一K线收盘价（时间戳严格小于当前K线）
    prev_c = None
    with db_lock:
        cur = db_conn.execute(
            "SELECT c FROM [kline] WHERE symbol=? AND interval=? AND t < ? ORDER BY t DESC LIMIT 1",
            (full_symbol, interval, t_val),
        )
        row = cur.fetchone()
        if row:
            prev_c = row[0]
    if prev_c is not None:
        atr_val = compute_latest_atr(full_symbol, interval, candle["h"], candle["l"], candle["c"], prev_c, t_val)
        if atr_val is not None:
            candle["atr14"] = atr_val
    upsert_candle(full_symbol, interval, candle)
    total = retention_cleanup(full_symbol, interval)
    update_status(full_symbol, interval, "add", total)
    update_data_time(full_symbol, interval)
    flush_status()


def upsert_candle_with_indicators(symbol, interval, candle):
    """upsert 单根K线并增量更新 ema20/atr14（复用 handle_candle_update 的指标逻辑）。
    供测试网 REST 轮询兜底使用；调用方需保证 candle 按时间升序传入。"""
    ema_val = compute_latest_ema(symbol, interval, str(candle["c"]), candle["t"])
    if ema_val is not None:
        candle["ema20"] = ema_val
    prev_c = None
    with db_lock:
        cur = db_conn.execute(
            "SELECT c FROM [kline] WHERE symbol=? AND interval=? AND t < ? ORDER BY t DESC LIMIT 1",
            (symbol, interval, candle["t"]),
        )
        row = cur.fetchone()
        if row:
            prev_c = row[0]
    if prev_c is not None:
        atr_val = compute_latest_atr(symbol, interval, candle["h"], candle["l"], candle["c"], prev_c, candle["t"])
        if atr_val is not None:
            candle["atr14"] = atr_val
    upsert_candle(symbol, interval, candle)
    total = retention_cleanup(symbol, interval)
    update_status(symbol, interval, "add", total)
    update_data_time(symbol, interval)


def rest_poll_loop():
    """测试网主采集兜底：WS 当前不可达（10054），用 REST 轮询保证数据持续新鲜。
    仅测试网模式启用；与 300s REST 校准并存（轮询保主数据，校准保已收盘精确）。"""
    logger.info("测试网 REST 轮询线程启动 (间隔 %ds)", TESTNET_POLL_INTERVAL)
    while running:
        try:
            for entry in get_symbol_intervals():
                for interval in entry["intervals"]:
                    try:
                        historical = fetch_historical(entry["name"], interval, 5)
                        if not historical:
                            continue
                        historical.sort(key=lambda c: c["t"])  # 升序，保证增量指标正确
                        for candle in historical:
                            upsert_candle_with_indicators(entry["name"], interval, candle)
                    except Exception as e:
                        logger.error("测试网 REST 轮询异常 %s %s: %s", entry["name"], interval, e)
                    time.sleep(0.2)
        except Exception as e:
            logger.error("测试网 REST 轮询循环异常: %s", e)
        for _ in range(TESTNET_POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)


def on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception:
        return
    channel = data.get("channel", "")
    event = data.get("event", "")

    if channel == "futures.positions" and event == "update":
        result = data.get("result", [])
        if isinstance(result, dict):
            result = [result]
        if isinstance(result, list):
            with account_lock:
                account_state["positions"] = result
            # 实时写入 current 表 + history 表
            save_position_history(result)
            for pos in result:
                contract = pos.get("contract", "")
                size = pos.get("size", "0")
                if size != "0":
                    mode = pos.get("mode", "")
                    entry = pos.get("entry_price", "0")
                    mark = pos.get("mark_price", "0")
                    pnl = pos.get("unrealised_pnl", "0")
                    lever = pos.get("leverage", "0")
                    logger.info("持仓更新: %s %s 数量=%s 入场=%s 标记=%s 浮盈=%s 杠杆=%sx", contract, mode, size, entry, mark, pnl, lever)
        return

    if channel == "futures.balances" and event == "update":
        result = data.get("result", {})
        if isinstance(result, dict):
            with account_lock:
                account_state["balances"] = result
            # 实时写入 current 表 + history 表
            save_balance_history(result)
        return

    if channel == "futures.orders" and event in ("put", "update", "finish"):
        result = data.get("result", {})
        if isinstance(result, dict):
            order = result
        elif isinstance(result, list) and result:
            order = result[0]
        else:
            return
        contract = order.get("contract", "")
        order_id = order.get("id", "")
        size = order.get("size", "0")
        price = order.get("price", "0")
        left = order.get("left", "0")
        status = order.get("status", "")
        save_order_history(order, event)
        if event == "put":
            with account_lock:
                account_state["pending_orders"].append(order)
            direction = "long" if int(size) > 0 else "short"
            logger.info("新挂单: %s %s 价格=%s 数量=%s", contract, direction, price, size)
        elif event == "update":
            with account_lock:
                for i, o in enumerate(account_state["pending_orders"]):
                    if str(o.get("id", "")) == str(order_id):
                        account_state["pending_orders"][i] = order
                        break
        elif event == "finish":
            with account_lock:
                account_state["pending_orders"] = [
                    o for o in account_state["pending_orders"]
                    if str(o.get("id", "")) != str(order_id)
                ]
            logger.info("挂单完成: %s 状态=%s", contract, status)
        return

    if channel == "futures.autoorders" and event in ("put", "update", "finish"):
        result = data.get("result", {})
        if isinstance(result, dict):
            order = result
        elif isinstance(result, list) and result:
            order = result[0]
        else:
            return
        contract = order.get("contract", "")
        order_id = order.get("id", "")
        trigger_price = order.get("trigger_price", "0")
        order_price = order.get("price", "0")
        size = order.get("size", "0")
        status = order.get("status", "")
        rule = order.get("trigger_rule", "")
        is_stop = order.get("is_stop_order", False)
        order_type = "止损单" if is_stop else "计划委托"
        save_price_order(order, event)
        if event == "put":
            with account_lock:
                account_state["auto_orders"].append(order)
            direction = "long" if int(size) > 0 else "short"
            logger.info("新%s: %s %s 触发价=%s 委托价=%s 数量=%s", order_type, contract, direction, trigger_price, order_price, size)
        elif event == "update":
            with account_lock:
                for i, o in enumerate(account_state["auto_orders"]):
                    if str(o.get("id", "")) == str(order_id):
                        account_state["auto_orders"][i] = order
                        break
        elif event == "finish":
            with account_lock:
                account_state["auto_orders"] = [
                    o for o in account_state["auto_orders"]
                    if str(o.get("id", "")) != str(order_id)
                ]
            logger.info("%s完成: %s 状态=%s", order_type, contract, status)
        return

    if channel == "futures.usertrades" and event == "update":
        result = data.get("result", [])
        if isinstance(result, dict):
            result = [result]
        if isinstance(result, list):
            save_trade_history(result)
            for trade in result:
                contract = trade.get("contract", "")
                size = trade.get("size", "0")
                price = trade.get("price", "0")
                role = trade.get("role", "")
                pnl = trade.get("realised_pnl", "0")
                fee = trade.get("fee", "0")
                direction = "long" if int(size) > 0 else "short"
                logger.info("成交: %s %s 价格=%s 数量=%s 角色=%s 盈亏=%s 手续费=%s", contract, direction, price, size, role, pnl, fee)
        return

    if channel == get_ticker_channel():
        if event == "subscribe":
            logger.info("Ticker subscribed: %s", data.get('payload', []))
            return
        if event == "update":
            result = data.get("result", [])
            if isinstance(result, list):
                for item in result:
                    contract = item.get("contract", "")
                    last = item.get("last", "")
                    if contract and last:
                        with prices_lock:
                            latest_prices[contract.upper()] = last
            return

    if channel != get_channel():
        return
    if event == "subscribe":
        logger.info("Subscribed: %s", data.get('payload', []))
        return
    if event == "update":
        result = data.get("result", {})
        if isinstance(result, list):
            for item in result:
                n_field = item.get("n", "")
                if n_field:
                    handle_candle_update(n_field, item)
        else:
            n_field = result.get("n", "")
            if n_field:
                handle_candle_update(n_field, result)


def on_error(ws, error):
    logger.error("WebSocket error: %s", error)


def on_close(ws, close_status_code, close_msg):
    global last_disconnect_time, downtime_gaps
    last_disconnect_time = int(time.time())
    downtime_gaps = {}
    for entry in get_symbol_intervals():
        for interval in entry["intervals"]:
            key = f"{entry['name']}_{interval}"
            downtime_gaps[key] = last_disconnect_time
    logger.warning("WebSocket closed at %d (%s): %s %s", last_disconnect_time, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_disconnect_time)), close_status_code, close_msg)


def on_open(ws):
    global last_disconnect_time, downtime_gaps
    logger.info("WebSocket connected")
    channel = get_channel()
    ticker_channel = get_ticker_channel()
    for entry in get_symbol_intervals():
        for interval in entry["intervals"]:
            payload = [interval, entry["name"]]
            req = {
                "time": int(time.time()),
                "channel": channel,
                "event": "subscribe",
                "payload": payload,
            }
            ws.send(json.dumps(req))
            logger.info("Subscribing %s %s", entry['name'], interval)
        req = {
            "time": int(time.time()),
            "channel": ticker_channel,
            "event": "subscribe",
            "payload": [entry["name"]],
        }
        ws.send(json.dumps(req))
        logger.info("Subscribing ticker %s", entry['name'])
    if last_disconnect_time > 0:
        disconnect_duration = int(time.time()) - last_disconnect_time
        logger.info("检测到断线重连，断线时长: %d秒 (%s)，开始补全数据...", disconnect_duration, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_disconnect_time)))
        threading.Timer(15, fill_gaps_during_downtime).start()
    else:
        logger.info("首次连接，无需数据补全")
    if watchlist_config.get("account_push") and watchlist_config.get("api_key"):
        api_key = watchlist_config["api_key"]
        api_secret = watchlist_config["api_secret"]
        ts = int(time.time())
        for ch in ["futures.positions", "futures.balances", "futures.orders", "futures.autoorders", "futures.usertrades"]:
            auth_sign = gate_sign(ch, "subscribe", ts, api_secret)
            sub_msg = {
                "time": ts,
                "channel": ch,
                "event": "subscribe",
                "payload": ["!all"],
                "auth": {"method": "api_key", "KEY": api_key, "SIGN": auth_sign},
            }
            ws.send(json.dumps(sub_msg))
            logger.info("Subscribing private channel: %s", ch)


def fill_gaps_during_downtime():
    global last_disconnect_time, downtime_gaps
    current_time = int(time.time())
    is_reconnect = last_disconnect_time > 0
    
    if is_reconnect:
        disconnect_duration = current_time - last_disconnect_time
        logger.info("开始断线重连数据补全 | 断线时间: %s | 断线时长: %d秒", 
                   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_disconnect_time)), 
                   disconnect_duration)
    else:
        logger.info("开始常规数据补全 (非断线重连)")
    
    total_filled = 0
    total_gaps_detected = 0
    total_validation_passed = 0
    total_validation_failed = 0
    
    for entry in get_symbol_intervals():
        for interval in entry["intervals"]:
            try:
                symbol = entry["name"]
                key = f"{symbol}_{interval}"
                
                # 获取断线时间点
                disconnect_ts = downtime_gaps.get(key, last_disconnect_time)
                
                # 计算时间间隔（秒）
                interval_seconds = {
                    "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400
                }.get(interval, 300)
                
                # 获取最近的数据用于检测缺失
                recent_data = list_recent(symbol, interval, 10)
                if recent_data:
                    last_ts = recent_data[-1]["t"]
                    expected_gaps = (current_time - last_ts) // interval_seconds - 1
                    if expected_gaps > 0:
                        total_gaps_detected += expected_gaps
                        logger.info("检测到 %s %s 缺失 %d 根K线 (最后更新: %s)", 
                                  symbol, interval, expected_gaps,
                                  time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_ts)))
                
                # 获取历史数据
                limit = get_max_candles()
                historical = fetch_historical(symbol, interval, limit)
                if not historical:
                    logger.warning("无法获取 %s %s 历史数据", symbol, interval)
                    continue
                
                # 过滤出断线期间的数据
                if is_reconnect and disconnect_ts > 0:
                    filtered_candles = [c for c in historical if c["t"] >= disconnect_ts - interval_seconds]
                    if filtered_candles:
                        logger.info("断线期间 %s %s 需补全 %d 根K线", symbol, interval, len(filtered_candles))
                else:
                    filtered_candles = historical
                
                # 插入数据
                market_type = get_market_type()
                filled_count = 0
                for candle in filtered_candles:
                    checked_candle = check_and_switch_source(symbol, interval, candle, market_type)
                    if checked_candle is not None:
                        candle = checked_candle
                    upsert_candle(symbol, interval, candle)
                    filled_count += 1
                
                # 重新计算指标
                recalculate_all_ema(symbol, interval)
                
                # 验证补全结果
                final_data = list_all_sorted(symbol, interval)
                final_count = len(final_data)
                
                # 验证连续性
                validation_passed = True
                if len(final_data) >= 2:
                    for i in range(1, len(final_data)):
                        time_diff = final_data[i]["t"] - final_data[i-1]["t"]
                        if time_diff != interval_seconds and time_diff != 0:
                            logger.warning("数据连续性验证失败 %s %s: 时间间隔异常 %d秒 (预期 %d秒)", 
                                         symbol, interval, time_diff, interval_seconds)
                            validation_passed = False
                            break
                
                if validation_passed:
                    total_validation_passed += 1
                else:
                    total_validation_failed += 1
                
                # 更新状态
                update_status(symbol, interval, "set_total", final_count)
                total_filled += filled_count
                
                # 增强日志输出
                logger.info("补全完成 %s %s | 新增: %d根 | 总计: %d根 | 验证: %s | 断线时间点: %s", 
                          symbol, interval, filled_count, final_count,
                          "通过" if validation_passed else "失败",
                          time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(disconnect_ts)) if disconnect_ts > 0 else "N/A")
                
            except Exception as e:
                logger.error("补全异常 %s %s: %s", entry['name'], interval, e, exc_info=True)
            time.sleep(0.3)
    
    # 清理断线时间记录
    if is_reconnect:
        last_disconnect_time = 0
        downtime_gaps = {}
    
    # 输出补全汇总
    logger.info("数据补全汇总 | 新增K线: %d | 检测缺失: %d | 验证通过: %d | 验证失败: %d | 模式: %s", 
              total_filled, total_gaps_detected, total_validation_passed, total_validation_failed,
              "断线重连" if is_reconnect else "常规补全")


def ws_thread_func():
    global ws_app
    ws_url = get_ws_url()
    while running:
        ws_app = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        try:
            ws_app.run_forever(ping_interval=10, ping_timeout=5)
        except Exception as e:
            logger.error("WebSocket exception: %s", e)
        if not running:
            break
        logger.info("Waiting 5s before reconnect...")
        for _ in range(50):
            if not running:
                break
            time.sleep(0.1)
        if running:
            fill_gaps_during_downtime()


def signal_handler(sig, frame):
    global running
    logger.info("Shutting down...")
    running = False
    if ws_app:
        try:
            ws_app.close()
        except Exception:
            pass


def print_summary():
    logger.info("=== SUMMARY ===")
    for entry in get_symbol_intervals():
        for interval in entry["intervals"]:
            data = list_all_sorted(entry["name"], interval)
            sc = status_counters.get(f"{entry['name']}_{interval}", {})
            logger.info("  %s %s: %d candles in DB", entry['name'], interval, len(data))
    with prices_lock:
        if latest_prices:
            logger.info("=== LATEST PRICES ===")
            for entry in get_symbol_intervals():
                name = entry["name"].upper()
                price = latest_prices.get(name, "N/A")
                logger.info("  %s: %s", name, price)
    
    backup_status = get_backup_status()
    if backup_status:
        logger.info("=== BACKUP SOURCE STATUS ===")
        for key, active in backup_status.items():
            logger.info("  %s: %s", key, "CLI 备用源已激活" if active else "主数据源")
    
    with account_lock:
        positions = account_state["positions"]
        balances = account_state["balances"]
        pending = account_state["pending_orders"]
    active_positions = [p for p in positions if p.get("size", "0") != "0"]
    if active_positions or pending or balances:
        logger.info("=== ACCOUNT ===")
        if balances:
            logger.info("  Balance: %s USDT (avail: %s)", balances.get('total', 'N/A'), balances.get('available', 'N/A'))
        for pos in active_positions:
            logger.info("  Position: %s %s size=%s entry=%s pnl=%s", pos.get('contract', ''), pos.get('mode', ''), pos.get('size', '0'), pos.get('entry_price', '0'), pos.get('unrealised_pnl', '0'))
        if pending:
            logger.info("  Pending Orders: %d", len(pending))
            for order in pending:
                sz = int(order.get("size", "0"))
                d = "long" if sz > 0 else "short"
                logger.info("    %s %s price=%s size=%d", order.get('contract', ''), d, order.get('price', '0'), abs(sz))


def initial_load():
    for entry in get_symbol_intervals():
        for interval in entry["intervals"]:
            try:
                limit = get_max_candles()
                historical = fetch_historical(entry["name"], interval, limit)
                if not historical:
                    continue
                market_type = get_market_type()
                for candle in historical:
                    checked_candle = check_and_switch_source(entry["name"], interval, candle, market_type)
                    if checked_candle is not None:
                        candle = checked_candle
                    upsert_candle(entry["name"], interval, candle)
                recalculate_all_ema(entry["name"], interval)
                total = len(list_all_sorted(entry["name"], interval))
                update_status(entry["name"], interval, "set_total", total)
                logger.info("Historical %s %s: %d total (%d loaded)", entry['name'], interval, total, len(historical))
            except Exception as e:
                logger.error("Initial load error %s %s: %s", entry['name'], interval, e)
            time.sleep(0.3)


def get_system_status():
    status = {
        "status": "running" if running else "stopped",
        "env": CURRENT_ENV,
        "env_label": "模拟盘" if CURRENT_ENV == "testnet" else "实盘",
        "market_type": get_market_type(),
        "symbols": [],
        "latest_prices": {},
        "account": {
            "positions_count": 0,
            "balances": {},
            "pending_orders_count": 0,
            "auto_orders_count": 0
        },
        "timestamp": int(time.time())
    }
    
    for entry in get_symbol_intervals():
        symbol_status = {
            "name": entry["name"],
            "available": True,
            "intervals": {}
        }
        for interval in entry["intervals"]:
            key = f"{entry['name']}_{interval}"
            with status_lock:
                sc = status_counters.get(key, {"added": 0, "total": 0})
                symbol_status["intervals"][interval] = {
                    "total_candles": sc["total"],
                    "last_added": sc["added"]
                }
        status["symbols"].append(symbol_status)

    # 当前环境无合约的品种（存在性校验结果），标注不可用
    for entry in watchlist_config.get("_unavailable_symbols", []):
        status["symbols"].append({
            "name": entry["name"],
            "available": False,
            "reason": entry.get("reason", "当前环境无此合约"),
        })
    
    with prices_lock:
        for symbol_name, price in latest_prices.items():
            status["latest_prices"][symbol_name] = price
    
    with account_lock:
        active_positions = [p for p in account_state["positions"] if p.get("size", "0") != "0"]
        status["account"]["positions_count"] = len(active_positions)
        status["account"]["balances"] = account_state["balances"]
        status["account"]["pending_orders_count"] = len(account_state["pending_orders"])
        status["account"]["auto_orders_count"] = len(account_state["auto_orders"])
    # 账户推送健康（配置意图/生效/成功年龄/停滞）——注意 _account_health
    # 自取 account_lock，必须在 with 块外调用（Lock 不可重入）
    status["account"]["health"] = _account_health()

    status["data_freshness"] = _summarize_freshness()
    status["aux_info"] = _get_aux_status()

    return status


def _summarize_freshness():
    """汇总各 symbol_interval 的数据新鲜度；全部停滞视为系统级数据中断"""
    try:
        feeds = get_monitor_status()
    except Exception:
        return {"total": 0, "stale": 0, "all_stale": False}
    stale_keys = [key for key, info in feeds.items() if info.get("is_stale")]
    return {
        "total": len(feeds),
        "stale": len(stale_keys),
        "stale_feeds": stale_keys[:50],
        "all_stale": bool(feeds) and len(stale_keys) == len(feeds),
    }


def _get_aux_status():
    """读取辅助信息流状态（fetch_aux 写入的 aux_status.json），供健康端点展示。
    辅助信息为旁路展示用途，不参与核心健康判定。"""
    try:
        from aux_monitor import read_aux_status
        return read_aux_status(os.path.join(SCRIPT_DIR, "aux-data", "aux_status.json"))
    except Exception as e:
        return {"present": False, "note": "读取失败: %s" % e}


def _account_health():
    """账户推送健康：配置/生效状态 + 成功年龄 + 停滞判定。

    - push_mismatch：配置开启但密钥缺失被强制关闭——密钥环境丢失的
      静默停更签名（08-26 事故：watchdog 无密钥重启后账户停更 20h
      而健康端点全绿，此盲区由此堵住）
    - stale：推送生效但超 3×间隔（≥180s）无成功采集；从未成功时
      以线程启动时刻起算同宽限，覆盖"线程跑起来但一次都没成"
    """
    configured = bool(watchlist_config.get("_account_push_configured"))
    enabled = bool(watchlist_config.get("account_push"))
    interval = int(watchlist_config.get("account_push_interval", 60) or 60)
    threshold = max(3 * interval, 180)
    now = time.time()
    with account_lock:
        last_ok = account_state.get("last_success_ts", 0)
        started = account_state.get("push_started_ts", 0)
    age = int(now - last_ok) if last_ok else None
    if not enabled:
        return {"configured": configured, "enabled": False,
                "push_mismatch": configured, "interval": interval,
                "last_success_age_s": age, "stale": False}
    stale = ((now - last_ok) > threshold) if last_ok else (
        bool(started) and (now - started) > threshold)
    return {"configured": configured, "enabled": True, "push_mismatch": False,
            "interval": interval, "last_success_age_s": age, "stale": bool(stale)}


def is_system_healthy():
    if not running:
        return False
    if not get_symbol_intervals():
        return False
    # 数据新鲜度：已有推送记录且全部停滞 => 判定不健康（总中断）。
    # 单一品种停滞（如贵金属周末休市）不视为系统故障。
    # 账户推送（配置开启时）：密钥失配被强制关闭、或采集停滞 => 不健康。
    try:
        if _summarize_freshness()["all_stale"]:
            return False
        ah = _account_health()
        return not (ah.get("stale") or ah.get("push_mismatch"))
    except Exception:
        return True


def parse_args():
    ap = argparse.ArgumentParser(description="K线数据采集（实盘/测试网双实例，数据隔离）")
    ap.add_argument("--env", choices=["live", "testnet"], default=None,
                    help="环境：live=实盘（默认）/ testnet=模拟盘；可用环境变量 PA_DATA_SOURCE_ENV 覆盖")
    ap.add_argument("--config", default="watchlist.yaml", help="watchlist 配置文件（默认 watchlist.yaml）")
    ap.add_argument("--db", default="data/kline.db", help="K线数据库路径（默认 data/kline.db）")
    ap.add_argument("--health-port", type=int, default=18080, help="健康检查端口（默认 18080）")
    ap.add_argument("--lock", default="kline_watcher.lock", help="单实例锁文件（默认 kline_watcher.lock）")
    return ap.parse_args()


def main():
    global running, WATCHLIST_PATH, KLINE_LOCK_FILE, HEALTH_PORT, DB_PATH, ACCOUNT_DB_PATH, DATA_DIR, CURRENT_ENV
    args = parse_args()
    # 环境解析：--env 优先，其次 PA_DATA_SOURCE_ENV 环境变量（向后兼容），最后默认 live
    CURRENT_ENV = (args.env or os.environ.get("PA_DATA_SOURCE_ENV", "live")).lower()
    # 路径/端口/锁文件由参数派生（实盘/测试网双实例隔离）
    WATCHLIST_PATH = os.path.join(SCRIPT_DIR, args.config)
    KLINE_LOCK_FILE = os.path.join(SCRIPT_DIR, args.lock)
    HEALTH_PORT = args.health_port
    DATA_DIR = os.path.join(SCRIPT_DIR, os.path.dirname(args.db) or "data")
    DB_PATH = os.path.join(SCRIPT_DIR, args.db)
    # 账户库随 K线库派生：kline.db → account.db；kline_testnet.db → account_testnet.db
    ACCOUNT_DB_PATH = os.path.join(DATA_DIR, os.path.basename(args.db).replace("kline", "account"))

    # 单实例锁：必须在任何 DB 写操作之前，防止双实例写同一 SQLite
    lock_handle, old_pid = acquire_single_instance_lock()
    if lock_handle is None:
        print("=" * 60)
        print("kline_watcher 已在运行 (PID {})".format(old_pid if old_pid else "未知"))
        print("健康状态: http://127.0.0.1:{}/health".format(HEALTH_PORT))
        print("状态查询: python status.py")
        print("=" * 60)
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    setup_logger(watchlist_config.get("log_level", "INFO"))
    logger.info("Loading %s...", args.config)
    load_config()
    # 合约存在性校验：跳过当前环境不存在的品种，不阻塞其他品种
    validate_contracts()
    resolved = get_symbol_intervals()
    if not resolved:
        logger.error("No symbols configured in %s", args.config)
        sys.exit(1)
    logger.info("Watchlist loaded: %d symbols (env=%s)", len(resolved), CURRENT_ENV)
    logger.info("Market type: %s", get_market_type())
    logger.info("Initializing SQLite...")
    init_db()
    init_account_db()
    logger.info("Fetching historical data via REST API...")
    initial_load()
    logger.info("Recalculating EMA20 for all tables...")
    for entry in resolved:
        for interval in entry["intervals"]:
            try:
                recalculate_all_ema(entry["name"], interval)
            except Exception as e:
                logger.error("EMA recalc error %s %s: %s", entry["name"], interval, e)
    logger.info("Running retention cleanup...")
    retention_cleanup()
    cleanup_account_data()
    print_summary()
    initial_account_load()
    if watchlist_config.get("account_push") and watchlist_config.get("api_key"):
        acc_thread = threading.Thread(target=account_push_loop, daemon=True)
        acc_thread.start()
        logger.info("账户推送线程已启动 (间隔 %ds)", watchlist_config.get("account_push_interval", 60))

    try:
        health_checker = start_health_check(port=HEALTH_PORT)
    except OSError as e:
        logger.error("健康检查端口 %d 绑定失败: %s", HEALTH_PORT, e)
        print("端口 {} 已被占用——通常意味着另一个 kline_watcher 实例正在运行".format(HEALTH_PORT))
        print("请先运行 python status.py 确认，或等待其退出后再启动")
        lock_handle.close()
        try:
            os.remove(KLINE_LOCK_FILE)
        except Exception:
            pass
        sys.exit(1)
    health_checker.register_status_provider(get_system_status, is_system_healthy)
    logger.info("Health check server started on port %d (env=%s)", HEALTH_PORT, CURRENT_ENV)

    # 测试网：WS 当前不可达（10054），启动 REST 轮询主采集兜底保证数据持续新鲜
    if CURRENT_ENV == "testnet":
        poll_thread = threading.Thread(target=rest_poll_loop, daemon=True)
        poll_thread.start()

    logger.info("Connecting WebSocket...")
    ws_thread = threading.Thread(target=ws_thread_func, daemon=True)
    ws_thread.start()
    stale_check_counter = 0
    reconcile_check_counter = 0
    try:
        while running:
            time.sleep(1)
            stale_check_counter += 1
            reconcile_check_counter += 1
            if stale_check_counter >= 60:
                stale_check_counter = 0
                try:
                    check_and_alert()
                except Exception as e:
                    logger.error("数据停滞检查异常: %s", e)
            if reconcile_check_counter >= REST_RECONCILE_INTERVAL_SEC:
                reconcile_check_counter = 0
                try:
                    reconcile_recent_completed_candles()
                except Exception as e:
                    logger.error("REST校准检查异常: %s", e)
    except KeyboardInterrupt:
        pass
    running = False
    logger.info("Stopping health check server...")
    health_checker = get_health_checker()
    health_checker.stop()
    logger.info("Closing WebSocket...")
    if ws_app:
        try:
            ws_app.close()
        except Exception:
            pass
    ws_thread.join(timeout=5)
    logger.info("Filling gaps before exit...")
    fill_gaps_during_downtime()
    print_summary()
    if db_conn:
        db_conn.close()
    if account_db_conn:
        account_db_conn.close()
    # 释放单实例锁
    try:
        lock_handle.close()
        os.remove(KLINE_LOCK_FILE)
    except Exception:
        pass
    logger.info("Exit graceful.")


if __name__ == "__main__":
    main()
