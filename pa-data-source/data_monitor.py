import time
import threading
from datetime import datetime
from logger import logger


class DataMonitor:
    def __init__(self, stale_threshold=120):
        self._last_update = {}
        self._lock = threading.Lock()
        self._stale_threshold = stale_threshold
        self._alerted_stale = set()

    def update(self, symbol, interval):
        key = f"{symbol}_{interval}"
        with self._lock:
            self._last_update[key] = {
                "timestamp": time.time(),
                "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol,
                "interval": interval,
            }
            self._alerted_stale.discard(key)

    def check_stale(self):
        now = time.time()
        stale_items = []
        with self._lock:
            for key, info in self._last_update.items():
                elapsed = now - info["timestamp"]
                if elapsed > self._stale_threshold:
                    stale_items.append({
                        "key": key,
                        "symbol": info["symbol"],
                        "interval": info["interval"],
                        "last_update": info["datetime"],
                        "elapsed_seconds": int(elapsed),
                    })
        return stale_items

    def log_stale_alerts(self):
        now = time.time()
        new_alerts = []
        # 检查与告警去重在同一次加锁内完成，避免两次加锁之间状态变化导致漏报/重复
        with self._lock:
            for key, info in self._last_update.items():
                elapsed = now - info["timestamp"]
                if elapsed > self._stale_threshold and key not in self._alerted_stale:
                    new_alerts.append({
                        "key": key,
                        "symbol": info["symbol"],
                        "interval": info["interval"],
                        "last_update": info["datetime"],
                        "elapsed_seconds": int(elapsed),
                    })
                    self._alerted_stale.add(key)
        for item in new_alerts:
            logger.warning(
                "数据停滞告警: %s %s 已 %d 秒未更新 (最后更新: %s)",
                item["symbol"],
                item["interval"],
                item["elapsed_seconds"],
                item["last_update"],
            )
        return new_alerts

    def get_status(self):
        now = time.time()
        status = {}
        with self._lock:
            for key, info in self._last_update.items():
                elapsed = now - info["timestamp"]
                status[key] = {
                    "symbol": info["symbol"],
                    "interval": info["interval"],
                    "last_update": info["datetime"],
                    "elapsed_seconds": int(elapsed),
                    "is_stale": elapsed > self._stale_threshold,
                }
        return status

    def get_symbol_status(self, symbol):
        now = time.time()
        status = {}
        with self._lock:
            for key, info in self._last_update.items():
                if info["symbol"] == symbol:
                    elapsed = now - info["timestamp"]
                    status[info["interval"]] = {
                        "last_update": info["datetime"],
                        "elapsed_seconds": int(elapsed),
                        "is_stale": elapsed > self._stale_threshold,
                    }
        return status

    def reset(self):
        with self._lock:
            self._last_update.clear()
            self._alerted_stale.clear()

    def reset_symbol(self, symbol):
        with self._lock:
            keys_to_remove = [k for k, v in self._last_update.items() if v["symbol"] == symbol]
            for key in keys_to_remove:
                del self._last_update[key]
                self._alerted_stale.discard(key)


monitor = DataMonitor(stale_threshold=120)


def update_data_time(symbol, interval):
    monitor.update(symbol, interval)


def check_and_alert():
    return monitor.log_stale_alerts()


def get_monitor_status():
    return monitor.get_status()
