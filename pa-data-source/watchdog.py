import subprocess
import sys
import os
import signal
import time
import logging
import socket
import shutil
import threading
from logging.handlers import TimedRotatingFileHandler
from collections import deque

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# monorepo root（worktree 或主仓）—— 逐所校验要借 omnialpha.exchanges 的适配器
ROOT = os.path.dirname(SCRIPT_DIR)
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "watchdog.log")

# 多所采集（kline_watcher_multi）的品种**不再写死在 args 里**：写死意味着加一个币要改
# 代码，而漏改的那处**不报错、只是永远没数据**。品种来自 watchlist.yaml
# （同一台机器上的品种权威，`multi_venue` 段声明非 Gate 五所的清单与周期）。
WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "watchlist.yaml")
# 兜底 = 改动前 args 里的实际清单（watchlist 读不到时行为不变，并告警）
DEFAULT_MULTI_SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "XRP_USDT"]
DEFAULT_MULTI_INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"]
MULTI_POLL_SEC = 30

# 多目标守护：kline_watcher（实盘+测试网双实例，数据隔离）+ 多所采集 + fetch_aux（辅助信息旁路，--loop 常驻）
# name 为唯一标识（进程字典/子进程日志按 name 区分），script 为脚本文件名
# 多所采集：写 kline_<ex>.db，schema 与 Gate kline.db 一致（contracts/KLINE_SCHEMA.md）；
# Gate 本体仍由 kline_watcher.py 负责，这里只采非 Gate 所，避免与 kline.db 双写。
# kline-multi 的 args 为 None = **启动时解析**（读 watchlist + 逐所校验可用性），
# import 本模块不发任何网络请求。
TARGETS = [
    {"name": "kline-live",    "script": "kline_watcher.py", "args": []},
    {"name": "kline-testnet", "script": "kline_watcher.py",
     "args": ["--env", "testnet", "--config", "watchlist_testnet.yaml",
              "--db", "data/kline_testnet.db", "--health-port", "18081",
              "--lock", "kline_watcher_testnet.lock"]},
    {"name": "kline-multi",   "script": "kline_watcher_multi.py", "args": None},
    {"name": "fetch-aux",     "script": "fetch_aux.py", "args": ["--loop"]},
]
LOCK_FILE = os.path.join(SCRIPT_DIR, "watchdog.lock")

RESTART_DELAY = 5
MAX_RESTARTS_PER_HOUR = 5

# 子进程 stdout 日志：按大小轮转，限制磁盘占用（防无界增长）
CHILD_LOG_MAX_BYTES = 50 * 1024 * 1024
CHILD_LOG_BACKUP_COUNT = 3

logger = logging.getLogger("watchdog")


# ── 多所采集品种：从 watchlist 读 + 逐所启动校验 ──────────────────────
def load_watchlist_config(path=None):
    """读 watchlist.yaml；读不到/损坏返回 `{}`（调用方各自回退，绝不阻断启动）。"""
    p = path or WATCHLIST_PATH
    try:
        import yaml
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if not isinstance(cfg, dict):
            logger.warning("watchlist %s 不是 mapping，忽略", p)
            return {}
        return cfg
    except Exception as e:  # noqa: BLE001 — 配置读不到不该让守护进程起不来
        logger.warning("watchlist 读取失败（%s: %s）", p, e)
        return {}


def _norm_symbols(seq):
    out = []
    for s in (seq or []):
        name = str(s or "").strip().upper()
        if name and name not in out:
            out.append(name)
    return out


def _multi_venue_block(cfg):
    mv = (cfg or {}).get("multi_venue")
    return mv if isinstance(mv, dict) else {}


def resolve_multi_symbols(cfg=None):
    """多所采集的品种清单 = `watchlist.multi_venue.symbols`（显式声明）。

    为什么另列一份：非 Gate 所没有 XAU/XAG（Gate 独有贵金属），要用六所都有的
    DOGE/XRP 补位 —— 这份差异是**声明**，不该埋在代码里。读不到就回退改动前的
    实际清单并告警（宁可少几个币，也不要守护进程起不来）。
    """
    syms = _norm_symbols(_multi_venue_block(cfg).get("symbols"))
    if syms:
        _hint_unlisted(cfg, syms)
        return syms
    logger.warning("多所采集: watchlist 里没有 multi_venue.symbols，回退内置默认 %s",
                   DEFAULT_MULTI_SYMBOLS)
    return list(DEFAULT_MULTI_SYMBOLS)


def _hint_unlisted(cfg, syms):
    """watchlist 有、但不在多所清单里的品种 → 提示（不是错误：Gate 独有品种本就如此）。"""
    outside = [s for s in _norm_symbols(
        (i.get("name") if isinstance(i, dict) else i) for i in ((cfg or {}).get("symbols") or []))
        if s not in syms]
    if outside:
        logger.info("多所采集: watchlist 里的 %s 不在多所清单内（要采就加进 "
                    "watchlist.multi_venue.symbols）", ", ".join(outside))


def resolve_multi_intervals(cfg=None):
    ivs = [str(i).strip() for i in (_multi_venue_block(cfg).get("intervals") or [])
           if str(i or "").strip()]
    return ivs or list(DEFAULT_MULTI_INTERVALS)


def _multi_exchanges():
    """要校验的交易所清单：取自 kline_watcher_multi 的默认（不在这里再写一份）。"""
    try:
        if SCRIPT_DIR not in sys.path:
            sys.path.insert(0, SCRIPT_DIR)
        from kline_watcher_multi import DEFAULT_EXCHANGES
        return list(DEFAULT_EXCHANGES)
    except Exception as e:  # noqa: BLE001
        logger.warning("多所采集: 读不到 kline_watcher_multi.DEFAULT_EXCHANGES（%s）", e)
        return []


def venue_listing(exchange):
    """该所当前可交易合约（内部写法 `BTC_USDT`）；**取不到/未接入 → None**。

    三态的理由见 `omnialpha/exchanges/coverage.py`：`None` 是「不知道」，不是「没有」。
    把「取不到」当空集，会把真实存在的币判成上币差异而放过。
    """
    name = str(exchange or "").strip().lower()
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from omnialpha.exchanges.registry import create_exchange
        client = create_exchange(name, env="live")
    except Exception as e:  # noqa: BLE001 — 校验是尽力而为，不阻断启动
        logger.info("品种可用性校验: %s 建 client 失败，跳过校验（%s）", name, e)
        return None
    fn = getattr(client, "available_symbols", None)
    if not callable(fn):
        logger.info("品种可用性校验: %s 未提供列合约能力，跳过校验", name)
        return None
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        logger.warning("品种可用性校验: %s 取合约清单失败，跳过校验（%s）", name, e)
        return None


def validate_multi_symbols(symbols, exchanges=None):
    """逐所校验品种是否存在（启动校验）→ 报告 + 保留清单。

    判据：**所有已校验的所都没有**该品种 → 从采集清单剔除（显式告警）；
    只在部分所缺 → 保留（单所缺币是上币差异，不该把币从全局摘掉）。
    一所都校验不了（离线/未接入）→ **一个都不剔**，如实说「未校验」。
    """
    syms = _norm_symbols(symbols)
    venues = [str(v).strip().lower() for v in (exchanges if exchanges is not None
                                               else _multi_exchanges()) if str(v or "").strip()]
    available, checked, unverified = {}, [], []
    for v in venues:
        got = venue_listing(v)
        if got is None:
            unverified.append(v)
        else:
            checked.append(v)
            available[v] = {str(s).strip().upper() for s in got}

    kept, dropped, confirmed_missing = [], [], {}
    for s in syms:
        miss = [v for v in checked if s not in available[v]]
        have = [v for v in checked if s in available[v]]
        if miss:
            for v in miss:
                confirmed_missing.setdefault(v, []).append(s)
        if checked and not have:
            dropped.append({"symbol": s, "reason": "已校验的所里都没有此合约: " + "/".join(miss)})
            logger.warning("多所采集: 品种 %s 在 %s 都没有此合约，跳过采集（上币差异？）",
                           s, "/".join(miss))
        else:
            kept.append(s)
            if miss:
                logger.warning("多所采集: 品种 %s 在 %s 没有此合约（该所跳过，其余所照采）",
                               s, "/".join(miss))

    if not checked:
        logger.info("多所采集: 品种可用性**未校验**（%s 都没给出合约清单），沿用配置清单",
                    "/".join(venues) or "无交易所")
    elif unverified:
        logger.info("多所采集: %s 未校验（未接入或取不到），只校验了 %s",
                    "/".join(unverified), "/".join(checked))
    return {"kept": kept, "dropped": dropped, "confirmed_missing": confirmed_missing,
            "checked": checked, "unverified": unverified}


def kline_multi_args(cfg=None, exchanges=None):
    """kline-multi 的命令行参数：品种/周期来自配置，可用性经逐所校验。"""
    syms = resolve_multi_symbols(cfg)
    ivs = resolve_multi_intervals(cfg)
    report = validate_multi_symbols(syms, exchanges)
    kept = report["kept"] or syms  # 全被剔除（不该发生）时宁可照采，不静默停采
    return ["--poll", str(MULTI_POLL_SEC),
            "--symbols", ",".join(kept),
            "--intervals", ",".join(ivs)]


def resolve_target_args():
    """启动时解析尚未解析的 args（import 本模块不得发网络请求）。"""
    for t in TARGETS:
        if t.get("args") is None:
            t["args"] = kline_multi_args()
            logger.info("kline-multi 品种来自配置: %s", " ".join(t["args"]))
    return TARGETS


def _python_has_deps(py):
    """探测解释器是否具备运行依赖（yaml / websocket）"""
    try:
        r = subprocess.run([py, "-c", "import yaml, websocket"], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def get_python_exe():
    """选择具备依赖的 Python 解释器（可移植）：环境变量 > 当前解释器 > PATH"""
    candidates = []
    env_py = os.environ.get("PA_DATA_SOURCE_PYTHON", "")
    if env_py and os.path.exists(env_py):
        candidates.append(env_py)
    if sys.executable and os.path.exists(sys.executable):
        candidates.append(sys.executable)
    for c in (shutil.which("python"), shutil.which("python3")):
        if c and c not in candidates:
            candidates.append(c)
    for py in candidates:
        if _python_has_deps(py):
            return py
    return candidates[0] if candidates else sys.executable


def _pid_alive(pid):
    """判断 PID 是否存活（跨平台）。Windows 用 GetExitCodeProcess 区分
    运行中与已终止但句柄未回收（僵尸）的进程，避免孤儿锁误判。"""
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
    """单实例锁：独占创建锁文件；锁文件存在且 PID 存活则视为已有实例（跨平台）"""
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
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


class ChildLogRotator:
    """子进程 stdout/stderr 日志：按大小轮转，限制磁盘占用（防无界增长）"""

    def __init__(self, base_path):
        self.base_path = base_path
        self._handle = None
        self._size = 0

    def open(self):
        self._handle = open(self.base_path, "ab", buffering=0)
        self._size = os.path.getsize(self.base_path)

    def write(self, data):
        if not data or self._handle is None:
            return
        self._handle.write(data)
        self._size += len(data)
        if self._size >= CHILD_LOG_MAX_BYTES:
            self.rotate()

    def rotate(self):
        if self._handle is None:
            return
        self._handle.close()
        for i in range(CHILD_LOG_BACKUP_COUNT - 1, 0, -1):
            src = f"{self.base_path}.{i}"
            dst = f"{self.base_path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        if os.path.exists(self.base_path):
            os.replace(self.base_path, f"{self.base_path}.1")
        self._handle = open(self.base_path, "ab", buffering=0)
        self._size = 0

    def close(self):
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:
                pass
            self._handle = None


class RestartRateLimiter:
    def __init__(self, max_restarts, window_seconds=3600):
        self.max_restarts = max_restarts
        self.window_seconds = window_seconds
        self.timestamps = deque()

    def can_restart(self):
        now = time.monotonic()
        while self.timestamps and self.timestamps[0] <= now - self.window_seconds:
            self.timestamps.popleft()
        return len(self.timestamps) < self.max_restarts

    def record(self):
        self.timestamps.append(time.monotonic())

    def remaining(self):
        now = time.monotonic()
        while self.timestamps and self.timestamps[0] <= now - self.window_seconds:
            self.timestamps.popleft()
        return max(0, self.max_restarts - len(self.timestamps))


class Watchdog:
    def __init__(self):
        self.processes = {}
        self._child_io = {}
        self.running = True
        self.limiter = RestartRateLimiter(MAX_RESTARTS_PER_HOUR)

    def start_all_processes(self):
        resolve_target_args()
        for target in TARGETS:
            self._start_one(target)

    def _start_one(self, target):
        name = target["name"]
        script = target["script"]
        # 上一次子进程异常退出时日志句柄可能未关闭，先释放避免句柄泄漏
        try:
            old_io = self._child_io.get(name)
            if old_io:
                old_io.close()
        except Exception:
            pass
        python_exe = get_python_exe()
        target_path = os.path.join(SCRIPT_DIR, script)
        logger.info("启动子进程: %s %s %s", python_exe, target_path, " ".join(target["args"]))
        # 子进程 stdout/stderr 走管道，由读取线程写入按大小轮转的日志，避免无界增长
        os.makedirs(LOG_DIR, exist_ok=True)
        child_log = os.path.join(LOG_DIR, name + "_child.log")
        rotator = ChildLogRotator(child_log)
        rotator.open()
        self._child_io[name] = rotator
        proc = subprocess.Popen(
            [python_exe, target_path] + target["args"],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self.processes[name] = proc
        threading.Thread(target=self._pipe_reader, args=(proc, rotator), daemon=True).start()
        logger.info("子进程已启动: %s PID=%d", name, proc.pid)

    def _pipe_reader(self, proc, rotator):
        try:
            for line in proc.stdout:
                rotator.write(line)
        except Exception as e:
            logger.error("读取子进程日志失败: %s", e)
        finally:
            rotator.close()

    def stop_all_processes(self):
        for name, proc in list(self.processes.items()):
            if proc.poll() is not None:
                continue
            logger.info("正在终止子进程 %s PID=%d ...", name, proc.pid)
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning("子进程 %s 未响应 SIGTERM, 强制终止 PID=%d", name, proc.pid)
                    proc.kill()
                    proc.wait(timeout=5)
            except OSError as e:
                logger.error("终止子进程 %s 失败: %s", name, e)
            try:
                io = self._child_io.get(name)
                if io:
                    io.close()
            except Exception:
                pass
            logger.info("子进程 %s 已终止", name)

    def shutdown(self):
        logger.info("收到退出信号, 守护进程关闭中...")
        self.running = False
        self.stop_all_processes()

    def run(self):
        setup_logger()
        logger.info("=" * 50)
        logger.info("watchdog 守护进程启动")
        # 启动校验：kline-multi 的品种来自 watchlist + 逐所可用性（不可用即告警并跳过）
        resolve_target_args()
        logger.info("监控目标: %s", ", ".join(t["name"] for t in TARGETS))
        logger.info("重启策略: 整体重启 | 重启延迟: %ds | 最大重启次数: %d次/小时",
                    RESTART_DELAY, MAX_RESTARTS_PER_HOUR)
        lock = acquire_single_instance_lock()
        if lock is None:
            logger.error("已有 watchdog 在运行（锁文件 %s），退出", LOCK_FILE)
            return
        logger.info("单实例锁: %s 已持有", LOCK_FILE)
        logger.info("=" * 50)

        signal.signal(signal.SIGINT, lambda *_: self.shutdown())
        signal.signal(signal.SIGTERM, lambda *_: self.shutdown())

        while self.running:
            self.start_all_processes()

            while self.running:
                dead = [name for name, p in self.processes.items()
                        if p.poll() is not None]
                if dead:
                    break
                time.sleep(1)

            if not self.running:
                break

            retcodes = {name: self.processes[name].poll() for name in dead}
            logger.warning("子进程退出: %s", retcodes)

            # 任何子进程退出（含 code 0，可能是锁冲突/配置错误等崩溃）都触发整体重启；
            # 守护进程只在收到 SIGINT/SIGTERM 时退出，绝不因子进程退出而自杀。
            self.stop_all_processes()

            if not self.limiter.can_restart():
                logger.error(
                    "已达重启上限 (%d次/小时), 暂停重启并保持存活告警",
                    MAX_RESTARTS_PER_HOUR,
                )
                # 不退出：保持 watchdog 存活并周期告警，限流窗口过去后自动恢复重启
                waited = 0
                while self.running and not self.limiter.can_restart():
                    time.sleep(10)
                    waited += 10
                    if waited % 600 == 0:
                        logger.error("子进程仍停止 (等待重启窗口), 已持续 %d 分钟", waited // 60)
                if not self.running:
                    break
                logger.info("重启窗口已恢复, 继续拉起子进程")

            self.limiter.record()
            remaining = self.limiter.remaining()
            logger.info(
                "将在 %ds 后整体重启 (本小时剩余重启次数: %d)",
                RESTART_DELAY, remaining,
            )

            for _ in range(RESTART_DELAY):
                if not self.running:
                    break
                time.sleep(1)

        logger.info("watchdog 守护进程已退出")
        try:
            lock.close()
        except Exception:
            pass
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    Watchdog().run()
