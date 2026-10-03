import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
# 日志文件可用 PA_KLINE_LOG 覆盖。多实例（kline-live + kline-testnet）默认会**共写**
# 同一个 `logs/kline_watcher.log` → 两个环境的消息交错，读日志时极易误判
# 「哪个环境在采什么」（实际发生过：看到实盘日志里有 DOGE/XRP 而以为实盘采错了品种，
# 其实是测试网的行）。各实例指各自的文件即可分开。
LOG_FILE = os.environ.get("PA_KLINE_LOG") or os.path.join(LOG_DIR, "kline_watcher.log")

logger = logging.getLogger("kline_watcher")


def setup_logger(log_level: str = "INFO"):
    if logger.handlers:
        return
    os.makedirs(os.path.dirname(LOG_FILE) or LOG_DIR, exist_ok=True)
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    # 无控制台环境（如 watchdog 后台子进程）兜底：stderr 不可用时写 devnull
    _console = sys.stderr if sys.stderr is not None else open(os.devnull, "w")
    console_handler = logging.StreamHandler(_console)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
