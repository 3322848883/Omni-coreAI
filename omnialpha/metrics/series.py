"""从 paper account.db 读取 equity / fills 序列（只读）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional


def _connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def load_equity_series(db_path: Path) -> list[tuple[int, float]]:
    """[(snap_time, equity)] 按时间升序。equity=balance+unrealised 若列存在。"""
    if not Path(db_path).exists():
        return []
    con = _connect_ro(Path(db_path))
    try:
        cur = con.execute("SELECT snap_time, equity, unrealised FROM pnl_snapshot ORDER BY snap_time")
        out = []
        for r in cur.fetchall():
            eq = float(r["equity"] or 0)
            if eq == 0 and r["unrealised"] is not None:
                # 容错：equity 列为 0 时跳过；不猜测 balance
                pass
            out.append((int(r["snap_time"] or 0), eq))
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def load_realised_trades(db_path: Path) -> list[dict[str, Any]]:
    """已实现盈亏成交（不含 fee 视为 round-trip 时用 realised_pnl）。"""
    if not Path(db_path).exists():
        return []
    con = _connect_ro(Path(db_path))
    try:
        cur = con.execute(
            "SELECT fill_time, contract, side, price, size, fee, realised_pnl, role, order_id "
            "FROM fills ORDER BY fill_time"
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def round_trip_pnls(fills: list[dict[str, Any]]) -> list[float]:
    """把 fills 中 realised_pnl!=0 的视为一次平仓（每笔 round-trip 的结果）。"""
    out = []
    for f in fills:
        rp = float(f.get("realised_pnl") or 0)
        if abs(rp) > 1e-12:
            out.append(rp)
    return out


def first_equity_or_initial(
    db_path: Path, initial_capital: float = 10000.0
) -> tuple[float, float, int]:
    """返回 (start_equity, end_equity, n_snaps)。"""
    series = load_equity_series(db_path)
    if not series:
        return float(initial_capital), float(initial_capital), 0
    return series[0][1], series[-1][1], len(series)


def oos_window_sharpe(
    series: list[tuple[int, float]], days: int = 30, periods_per_year: float = 365.0
) -> float:
    """近 N 日子样本 Sharpe（末段 equity）。"""
    from .stats import returns_from_equity, sharpe

    if len(series) < 3:
        return 0.0
    t_end = series[-1][0]
    cutoff = t_end - int(days) * 86400
    window = [eq for ts, eq in series if ts >= cutoff]
    if len(window) < 3:
        window = [eq for _, eq in series[-30:]]
    return sharpe(returns_from_equity(window), periods_per_year)
