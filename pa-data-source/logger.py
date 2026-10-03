import logging
import os
import sys
from logging.handlers import RotatingFileHandler

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
# 日志文件可用 PA_KLINE_LOG 覆盖。多实例（kline-live + kline-testnet）默认会**共写**
# 同一个 `logs/kline_watcher.log` → 两个环境的消息交错，读日志时极易误判
# 「哪个环境在采什么」（实际发生过：看到实盘日志里有 DOGE/XRP 而以为实盘采错了品种，
# 其实是测试网的行）。各实例指各自的文件即可分开。
LOG_FILE = os.environ.get("PA_KLINE_LOG") or os.path.join(LOG_DIR, "kline_watcher.log")
# 轮转策略：**按大小**而不是按天。
# 原来用 `TimedRotatingFileHandler(when="midnight", backupCount=7)` —— 只有时间维度，
# 一天能长多大完全不受控。实测（修复覆盖摘要刷屏之前）199MB/6h ≈ 541MB/天
# ⇒ 峰值可达数 GB。改成按大小轮转后**磁盘占用有确定上界**：
#   64MB × (1 当前 + 5 备份) ≈ 384MB
# 代价：不再能「按日期找昨天的日志」。但覆盖摘要改为变化时才打之后（见
# kline_watcher.flush_status），日增量降到 ~20MB，实际很少触发轮转。
LOG_MAX_BYTES = int(os.environ.get("PA_KLINE_LOG_MAX_BYTES") or 64 * 1024 * 1024)
LOG_BACKUP_COUNT = int(os.environ.get("PA_KLINE_LOG_BACKUPS") or 5)

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
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
