"""进程看门狗：守护全部 bot 的 plan-loop / paper-run / run，挂了自动拉起 + 飞书通知。

与 supervisor 的分工：
  - supervisor  —— 托管一组 bot 的 plan+run（实盘常用），子进程崩溃重启
  - watchdog    —— 全局视角扫「应有进程」，发现缺失就补拉（含 paper-run）

判定「在不在」：探 OS 文件锁（worker 持锁）；锁空 → 视为挂了。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("omnialpha.watchdog")

# 组件 → CLI 子命令
COMPONENT_CMD = {
    "plan": "plan-loop",
    "run": "run",
    "paper": "paper-run",
}


@dataclass
class Target:
    bot_id: str
    component: str  # plan | run | paper
    restarts: list[float] = field(default_factory=list)
    stopped: bool = False
    skip_until: float = 0.0


class Watchdog:
    def __init__(
        self,
        root: Path,
        bot_ids: Optional[list[str]] = None,
        interval_sec: float = 15.0,
        max_restarts_per_hour: int = 5,
        notify: bool = True,
    ):
        self.root = Path(root).resolve()
        self.bot_ids = bot_ids
        self.interval_sec = float(interval_sec)
        self.max_restarts = int(max_restarts_per_hour)
        self.notify = notify
        self.targets: list[Target] = []
        self._stop = False
        self._notifier = None

    # ── 目标发现 ──────────────────────────────────
    def discover(self) -> list[Target]:
        """只守护 enabled: true 的 bot（每个 bot yaml 的开关）。

        - enabled: false → 跳过，绝不拉起
        - enabled: true  → 挂了自动补拉
        - self.bot_ids   → 再收窄到指定 id（可选）
        """
        from .config import load_all_bots

        bots = load_all_bots(self.root / "config" / "bots")
        self.targets = []
        for bid, cfg in bots.items():
            if not getattr(cfg, "enabled", True):
                continue  # 开关关着 = 不管
            if self.bot_ids is not None and bid not in self.bot_ids:
                continue
            env = (getattr(cfg, "env", "") or "").lower()
            runtime = dict(getattr(cfg, "strategist", {}) or {}).get("runtime") or {}
            # plan-loop：默认开
            if runtime.get("plan_loop", True):
                self.targets.append(Target(bid, "plan"))
            # 执行侧：paper → paper-run；其它 → run
            if env == "paper":
                if runtime.get("paper_run", True):
                    self.targets.append(Target(bid, "paper"))
            else:
                if runtime.get("run", True):
                    self.targets.append(Target(bid, "run"))
        return self.targets

    # ── 存活探测（OS 锁） ─────────────────────────
    @staticmethod
    def _alive(bot_id: str, component: str, root: Path) -> bool:
        """worker 持 OS 锁 = 活着。"""
        from .pidlock import PidLock

        if component == "plan":
            lock_path = root / "data" / "bots" / bot_id / "state" / "plan.lock"
        else:
            # run / paper 共用 run.lock
            lock_path = root / "data" / "bots" / bot_id / "state" / "run.lock"
        if not lock_path.exists():
            return False
        probe = PidLock(lock_path)
        got = probe.acquire()
        if got is None:
            return True  # 被持有 → 活着
        # 拿到了 = 没人在跑；立刻放掉，留给 worker
        got.release()
        return False

    # ── 重启 ──────────────────────────────────────
    def _allow_restart(self, t: Target) -> bool:
        now = time.time()
        t.restarts = [x for x in t.restarts if now - x < 3600]
        if len(t.restarts) >= self.max_restarts:
            t.stopped = True
            self._alert(
                f"{t.bot_id}/{t.component}：1 小时内重启 {self.max_restarts} 次，已停手",
                kind="storm", color="red",
                fields=[("Bot", t.bot_id), ("组件", t.component),
                        ("重启次数", str(self.max_restarts)), ("处置", "请人工介入")],
            )
            return False
        t.restarts.append(now)
        return True

    def _spawn(self, t: Target) -> bool:
        cmd_name = COMPONENT_CMD.get(t.component)
        if not cmd_name:
            return False
        py = self._windowless_python()
        args = [py, "-m", "omnialpha", "--root", str(self.root), cmd_name, "--bot", t.bot_id]
        log_dir = self.root / "data" / "bots" / t.bot_id / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        out_fh = open(log_dir / f"{t.component}.out", "ab")
        err_fh = open(log_dir / f"{t.component}.err", "ab")
        env = os.environ.copy()
        env.setdefault("OMNIALPHA_ROOT", str(self.root))
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEW_PROCESS_GROUP|NO_WINDOW
        else:
            env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            p = subprocess.Popen(
                args, cwd=str(self.root), env=env,
                stdout=out_fh, stderr=err_fh, creationflags=flags,
            )
            log.info("started %s/%s pid=%s", t.bot_id, t.component, p.pid)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("spawn %s/%s failed: %s", t.bot_id, t.component, e)
            return False

    @staticmethod
    def _windowless_python() -> str:
        exe = Path(sys.executable)
        if os.name == "nt":
            w = exe.parent / "pythonw.exe"
            if w.exists():
                return str(w)
        return str(exe)

    # ── 通知 ──────────────────────────────────────
    def _alert(self, text: str, kind: str = "info", fields: Optional[list] = None,
               color: str = "blue") -> None:
        log.warning(text)
        if not self.notify:
            return
        try:
            from .monitoring import notify_process_event

            notify_process_event(
                root=self.root, kind=kind,
                title=text.replace("[watchdog] ", ""),
                fields=fields or [], color=color,
            )
        except Exception:  # noqa: BLE001
            pass

    # ── 主循环 ────────────────────────────────────
    def check_once(self) -> dict:
        missing = []
        restarted = []
        held = []
        for t in self.targets:
            if t.stopped:
                continue
            if time.time() < t.skip_until:
                continue
            if self._alive(t.bot_id, t.component, self.root):
                held.append(f"{t.bot_id}/{t.component}")
                continue
            missing.append(f"{t.bot_id}/{t.component}")
            if self._allow_restart(t):
                if self._spawn(t):
                    restarted.append(f"{t.bot_id}/{t.component}")
                    self._alert(
                        f"自动拉起 {t.bot_id}/{t.component}",
                        kind="restart", color="green",
                        fields=[("Bot", t.bot_id), ("组件", t.component), ("动作", "已自动重启")],
                    )
                else:
                    t.skip_until = time.time() + 30.0
        return {"held": held, "missing": missing, "restarted": restarted}

    def run_forever(self) -> None:
        self.discover()
        if not self.targets:
            self._alert("watchdog: 无目标 bot，退出")
            return
        self._alert(
            f"看门狗已启动，接管 {len({t.bot_id for t in self.targets})} 个 bot / {len(self.targets)} 个组件",
            kind="start", color="blue",
            fields=[("Bot 数", str(len({t.bot_id for t in self.targets}))),
                    ("组件数", str(len(self.targets)))],
        )

        def _sig(*_a):
            self._stop = True

        try:
            import signal

            signal.signal(signal.SIGINT, _sig)
            signal.signal(signal.SIGTERM, _sig)
        except Exception:  # noqa: BLE001
            pass

        try:
            while not self._stop:
                try:
                    self.check_once()
                except Exception as e:  # noqa: BLE001
                    log.warning("check error: %s", e)
                time.sleep(self.interval_sec)
        finally:
            self._alert("看门狗已停止", kind="stop", color="grey", fields=[("状态", "已停止")])
