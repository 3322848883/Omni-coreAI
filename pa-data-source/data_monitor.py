"""K 线数据监控：两类告警，两个不同的病因。

- **停滞**（`log_stale_alerts`）：见过的 `(symbol, interval)` 超时没更新。
- **零数据**（`log_missing_alerts`）：**期望宇宙**里的品种一条数据都没有 —— 旧实现只
  跟踪见过的 key，没有 key 就没有循环，于是「新加的币 / 该所没上线的币」永远报不出来。

零数据检查需要**期望宇宙**（`expect_symbols()` 显式声明，或 `check_and_alert()` 惰性从
watchlist 推导）；没有期望宇宙时行为与改动前完全一致。
"""
import os
import sys
import time
import threading
from datetime import datetime
from logger import logger

# 期望宇宙刚声明时的宽限期：刚启动数据还没到，别把「还没开始采」报成「这个币零数据」。
# kline_watcher 的 REST 回填是 30–60s 量级，3 分钟足够宽。
MISSING_GRACE_SEC = 180
# watchlist 读不到时的重试间隔：`check_and_alert()` 每 60s 跑一次，失败不缓存会刷屏
EXPECTED_RETRY_SEC = 600
# 周期展示顺序（按时间尺度，而不是字母序 —— '15m,1d,1h,1m' 读起来是噪音）
TF_ORDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1w"]


def _sort_intervals(seq):
    ivs = [str(i).strip() for i in (seq or []) if str(i or "").strip()]
    return sorted(set(ivs), key=lambda x: (TF_ORDER.index(x) if x in TF_ORDER else len(TF_ORDER), x))


def _watchlist_path():
    """期望宇宙的默认来源：与采集器**同一份** watchlist。

    顺序：`OMNIALPHA_WATCHLIST`（与 fetch_aux 同名，便于统一指定）→ 命令行 `--config`
    （live/testnet 双实例靠它区分，否则 testnet 进程会拿 live 的清单去报「品种零数据」）
    → 本目录 watchlist.yaml。
    """
    env = os.environ.get("OMNIALPHA_WATCHLIST") or os.environ.get("AUX_WATCHLIST")
    if env:
        return env
    argv = list(sys.argv or [])
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return _local_if_exists(argv[i + 1])
        if a.startswith("--config="):
            return _local_if_exists(a.split("=", 1)[1])
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.yaml")


def _local_if_exists(p):
    """`--config` 多是相对路径：进程 cwd 不一定是本目录，先按本目录试一次。"""
    p = str(p or "")
    if not p or os.path.isabs(p):
        return p
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
    return local if os.path.exists(local) else p


class DataMonitor:
    def __init__(self, stale_threshold=120, missing_grace_sec=MISSING_GRACE_SEC):
        self._last_update = {}
        self._lock = threading.Lock()
        self._stale_threshold = stale_threshold
        self._alerted_stale = set()
        # 期望宇宙（按币）：只跟踪「见过的 key」时，一个从来没产出过数据的币
        # **永远进不了这个循环** → 也就永远不会有告警。新加的币、在该所没上线的币
        # 正是这种形态：整体 ok 掩盖「这个币零数据」。
        self._expected = {}
        self._expected_source = ""
        self._expected_at = 0.0
        self._expected_attempt_ts = 0.0
        self._missing_grace_sec = missing_grace_sec
        self._alerted_missing = set()

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
            self._alerted_missing.discard(symbol)

    # ── 按币覆盖（期望宇宙）──────────────────────────────────────
    def expect(self, symbols, intervals=None, source=""):
        """声明「期望有数据的品种」。返回归一后的品种清单（保序去重）。

        只有声明过期望宇宙，「零数据的币」才可能被报出来 —— 监控不能靠"见过什么"
        推断"应该有什么"。空清单视为**没有声明**（行为与改动前一致）。
        """
        syms = []
        for s in (symbols or []):
            name = str(s or "").strip().upper()
            if name and name not in syms:
                syms.append(name)
        ivs = _sort_intervals(intervals)
        with self._lock:
            if not syms:
                return []
            self._expected = {s: list(ivs) for s in syms}
            self._expected_source = str(source or "")
            self._expected_at = time.time()
            self._alerted_missing = set()
        return syms

    def ensure_expected(self, path=None):
        """惰性声明期望宇宙（没声明过时从 watchlist 推导）。

        读不到 / 解析失败 / 里面没有 symbols → 返回 False 并**不做任何新检查**：
        监控不该因为一个配置文件就报一片假警，更不该崩（宁可少一层观测）。
        """
        now = time.time()
        with self._lock:
            if self._expected:
                return True
            if now - self._expected_attempt_ts < EXPECTED_RETRY_SEC:
                return False  # 刚失败过：不重试、不重复告警（check_and_alert 每 60s 跑一次）
            self._expected_attempt_ts = now
        wl = path or _watchlist_path()
        syms, ivs = [], []
        try:
            import yaml  # 可选依赖：没装就不做这一层
            with open(wl, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for item in (cfg.get("symbols") or []):
                if isinstance(item, dict):
                    name, iv = item.get("name"), item.get("intervals")
                else:
                    name, iv = item, None
                name = str(name or "").strip().upper()
                if not name or name in syms:
                    continue
                syms.append(name)
                for one in (iv or []):
                    one = str(one or "").strip()
                    if one and one not in ivs:
                        ivs.append(one)
        except Exception as e:  # noqa: BLE001 — 读不到就不做这一层，绝不崩采集器
            logger.warning("按币覆盖检查: watchlist 读取失败（%s: %s），跳过", wl, e)
            return False
        if not syms:
            logger.warning("按币覆盖检查: watchlist %s 里没有 symbols，跳过", wl)
            return False
        self.expect(syms, ivs, source=str(wl))
        logger.info("按币覆盖检查: 期望宇宙 %d 个品种（来源 %s）", len(syms), wl)
        return True

    def expected_symbols(self):
        with self._lock:
            return list(self._expected)

    def missing_symbols(self):
        """期望宇宙里**一条数据都没有**的品种（宽限期内返回空）。"""
        now = time.time()
        with self._lock:
            if not self._expected or now - self._expected_at < self._missing_grace_sec:
                return []
            seen = {info["symbol"] for info in self._last_update.values()}
            return sorted(s for s in self._expected if s not in seen)

    def log_missing_alerts(self):
        """对**新**出现的「某币零数据」各告警一次；数据恢复后重新武装。"""
        missing = self.missing_symbols()
        new_items = []
        with self._lock:
            for sym in missing:
                if sym in self._alerted_missing:
                    continue
                self._alerted_missing.add(sym)
                new_items.append({
                    "symbol": sym,
                    "intervals": list(self._expected.get(sym) or []),
                    "expected_source": self._expected_source,
                })
        for item in new_items:
            logger.warning(
                "品种零数据告警: %s 没有任何 K 线数据（期望周期 %s，期望来源 %s）",
                item["symbol"],
                ",".join(item["intervals"]) or "-",
                item["expected_source"] or "-",
            )
        return new_items

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
            self._alerted_missing.clear()

    def reset_symbol(self, symbol):
        with self._lock:
            keys_to_remove = [k for k, v in self._last_update.items() if v["symbol"] == symbol]
            for key in keys_to_remove:
                del self._last_update[key]
                self._alerted_stale.discard(key)
            self._alerted_missing.discard(symbol)


monitor = DataMonitor(stale_threshold=120)


def update_data_time(symbol, interval):
    monitor.update(symbol, interval)


def expect_symbols(symbols, intervals=None, source=""):
    """声明期望宇宙（供采集器启动时调用：`expect_symbols(bot_symbols)`）。"""
    return monitor.expect(symbols, intervals=intervals, source=source)


def expect_from_watchlist(path=None):
    """从 watchlist 推导期望宇宙（显式声明优先，这里只是兜底来源）。"""
    return monitor.ensure_expected(path=path)


def missing_symbols():
    """期望宇宙里零数据的品种（监控/健康检查可读）。"""
    return monitor.missing_symbols()


def check_and_alert():
    """一次检查：停滞（见过的 key 超时）+ 零数据（期望宇宙里没出现过的币）。

    返回 `{"stale": [...], "missing": [...]}`（两者都是本轮**新增**的告警）。
    没声明过期望宇宙时惰性从 watchlist 推导；推导不出来就只做停滞检查 ——
    与改动前的行为一致，不新增任何告警。
    """
    monitor.ensure_expected()
    return {"stale": monitor.log_stale_alerts(),
            "missing": monitor.log_missing_alerts()}


def get_monitor_status():
    return monitor.get_status()
