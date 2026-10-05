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
    # plan | run | paper | persona | broadcast
    #   persona  → bot_id 形如 "persona:<group>"（组级进程，不属于任何单个 bot）
    #   broadcast → bot_id 固定 "broadcast"（全局进程）
    component: str
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
        # 健康体检：HealthMonitor.check() 此前全仓库无人调用 → 心跳/延迟/错误阈值
        # 全部形同虚设。看门狗是天然的周期监控点，在这里接上（带去重防刷屏）。
        self.health_interval_sec = 300.0
        self.health_realert_sec = 1800.0
        self._last_health_check = 0.0
        self._health_alerted: dict[str, float] = {}

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
        # 组级/全局进程 —— 原先**无人守护**（架构盘点 S17）：
        # `persona-run` 是讨论组的大脑，它挂了意味着「保护单有人管、但没人再决策」；
        # `broadcast` 挂了则信号不再扇出。两者此前都不在 watchdog 的清单里。
        self.targets.extend(self._discover_globals())
        return self.targets

    def _discover_globals(self) -> list[Target]:
        """组级（persona-run）与全局（broadcast）常驻进程。

        两条**显式 opt-in** 规则，都不是「存在即守护」：

          - persona：组要同时 `enabled: true` **且** `runtime.persona_run: true`。
            只看 `enabled` 不行 —— 本机 persona_groups.yaml 有 9 个组都是
            `enabled: true`（含 4 个对照实验组），按 `enabled` 守护会让本地
            一启动 watchdog 就拉起 9 个 persona-run，每个都在跑真实 LLM 分析。
          - broadcast：`config/broadcast.yaml` 里至少有一条 `enabled: true` 的路由。
            当前路由全关，守护它只是白起一个空转进程。

        只在 `bot_ids is None`（全量守护）时纳入 —— 显式指定了 bot 列表时，
        调用方要的是「只管这几个 bot」，不该顺带拉起全局进程。
        """
        out: list[Target] = []
        if self.bot_ids is not None:
            return out
        try:
            from .persona import load_persona_groups

            p = self.root / "config" / "persona_groups.yaml"
            if p.is_file():
                for g in load_persona_groups(p):
                    if not getattr(g, "enabled", False):
                        continue
                    if not (getattr(g, "runtime", {}) or {}).get("persona_run"):
                        continue
                    out.append(Target(f"persona:{g.name}", "persona"))
        except Exception as e:  # noqa: BLE001
            log.debug("discover persona groups failed: %s", e)
        try:
            if self._broadcast_has_enabled_route():
                out.append(Target("broadcast", "broadcast"))
        except Exception as e:  # noqa: BLE001
            log.debug("discover broadcast failed: %s", e)
        return out

    def _broadcast_has_enabled_route(self) -> bool:
        p = self.root / "config" / "broadcast.yaml"
        if not p.is_file():
            return False
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for r in (data.get("routes") or []):
            if isinstance(r, dict) and r.get("enabled"):
                return True
        return False

    # ── 存活探测（OS 锁） ─────────────────────────
    @staticmethod
    def _alive(bot_id: str, component: str, root: Path) -> bool:
        """worker 持 OS 锁 = 活着。"""
        from .pidlock import PidLock

        if component == "plan":
            lock_path = root / "data" / "bots" / bot_id / "state" / "plan.lock"
        elif component == "persona":
            group = bot_id.split(":", 1)[-1]
            lock_path = root / "data" / "shared" / f"persona-{group}.lock"
        elif component == "broadcast":
            lock_path = root / "data" / "shared" / "broadcast.lock"
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
        py = self._windowless_python()
        base = [py, "-m", "omnialpha", "--root", str(self.root)]
        if t.component == "persona":
            group = t.bot_id.split(":", 1)[-1]
            args = base + ["persona-run", "--group", group]
            log_dir = self.root / "data" / "shared" / "logs"
        elif t.component == "broadcast":
            args = base + ["broadcast"]
            log_dir = self.root / "data" / "shared" / "logs"
        else:
            cmd_name = COMPONENT_CMD.get(t.component)
            if not cmd_name:
                return False
            args = base + [cmd_name, "--bot", t.bot_id]
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
        finally:
            # Popen 已把 fd dup 给子进程，父进程必须关掉自己这两个句柄：
            # 不关会每次拉起泄漏 2 个 fd；Windows 上还会锁住日志文件，
            # 导致日志轮转/目录清理失败（PermissionError）。
            for fh in (out_fh, err_fh):
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass

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

    def _check_health(self) -> list[str]:
        """周期体检各 bot 的 health 记录，把 check() 的告警推出去（带去重）。

        同一告警在 health_realert_sec 内只推一次，避免每 15s 刷屏。
        """
        now = time.time()
        if now - self._last_health_check < self.health_interval_sec:
            return []
        self._last_health_check = now
        try:
            from .monitoring import HealthMonitor
        except Exception:  # noqa: BLE001
            return []
        sent: list[str] = []
        for bid in sorted({t.bot_id for t in self.targets}):
            try:
                alerts = HealthMonitor(self.root, bid).check()
            except Exception:  # noqa: BLE001
                continue
            for msg in alerts:
                if "no heartbeat yet" in msg:
                    continue  # 刚启动还没跑过一轮，不算异常
                if now - self._health_alerted.get(msg, 0.0) < self.health_realert_sec:
                    continue
                self._health_alerted[msg] = now
                sent.append(msg)
                self._alert(
                    f"[health] {bid}: {msg}",
                    kind="health", color="orange",
                    fields=[("Bot", bid), ("健康告警", msg)],
                )
        return sent

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
        health = self._check_health()
        return {"held": held, "missing": missing, "restarted": restarted,
                "health_alerts": health}

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
