# -*- coding: utf-8 -*-
"""吞异常的可观测性：**吞可以，但必须留痕 + 计数**。

背景（2026-10-14 全面检查 N3）：全仓有 180+ 处 `except Exception:` 后直接
`pass` / `return None|[]|{}`。绝大多数是**刻意的**「增强不阻断主流程」（记忆、
行情、告警这些不该把整轮决策打掉），问题不在「吞」本身，而在**静默** ——
「本该有数据却拿到空」事后无从发现。

实测先例（都是静默吞出来的坑）：
  · `auto_protect_sl_pct <= 0` 时 `ensure_protection` 直接早退，dry 观察期其实
    什么都没观察到（连「本来会挂什么」都不算）；
  · 账户取数失败被吞成空 `positions`，模型看到「无持仓」而非「取数失败」。

用法（**不改控制流**，返回值仍由调用方决定）::

    from ..observability import swallow

    try:
        rows = self.client.get_positions() or []
    except Exception as e:                      # noqa: BLE001
        swallow("executor._symbol_positions", e, level=logging.WARNING)
        return []

约定：
  · 关键路径（执行器 / 账户与持仓读取 / 闸门 / 留痕）→ `level=logging.WARNING`；
  · 辅助路径（画像、行情增强、通知）→ 默认 `DEBUG`（不刷日志，但**计数**照记）；
  · `counts()` 暴露累计计数，供 health / 巡检读取（`omnialpha observability`）。

设计约束：零依赖、线程安全（`Lock`）、进程内计数（不落盘 —— 落盘失败本身就是
一个要吞的异常，会让这个模块自己变成新的静默源）。
"""
from __future__ import annotations

import logging
import threading
from collections import Counter

log = logging.getLogger("omnialpha.observability")

_COUNTS: Counter[str] = Counter()
_LOCK = threading.Lock()

#: 允许的级别（防止调用方传一个非法值导致 logging 抛错 —— 那会从「吞」变成「炸」）
_ALLOWED = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR)


def swallow(where: str, exc: BaseException | str, *,
            level: int = logging.DEBUG, note: str = "") -> None:
    """记录一次「被吞掉的异常」。**绝不抛异常**（自身失败也必须静默）。

    `where` 用 `模块.函数` 形式（如 `"executor._symbol_positions"`），它同时是计数键，
    所以要保持稳定 —— 巡检看到的就是「哪个位置吞了多少次」。
    """
    try:
        key = str(where or "unknown")
        with _LOCK:
            _COUNTS[key] += 1
        lvl = level if level in _ALLOWED else logging.DEBUG
        msg = "swallowed at %s: %r" % (key, exc)
        if note:
            msg += f" ({note})"
        log.log(lvl, msg)
    except Exception:  # noqa: BLE001 — 可观测性自身绝不冒泡
        pass


def counts() -> dict[str, int]:
    """累计计数快照（按次数降序）。"""
    with _LOCK:
        return dict(_COUNTS.most_common())


def total() -> int:
    with _LOCK:
        return sum(_COUNTS.values())


def reset_counts() -> None:
    """清空计数（测试用）。"""
    with _LOCK:
        _COUNTS.clear()
