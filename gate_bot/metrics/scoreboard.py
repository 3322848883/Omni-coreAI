"""聚合各 bot paper 账本 → 评分板。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from .series import (
    first_equity_or_initial,
    load_equity_series,
    load_realised_trades,
    oos_window_sharpe,
    round_trip_pnls,
)
from .stats import (
    calmar,
    deflated_sharpe,
    expectancy,
    kurtosis,
    max_drawdown_pct,
    profit_factor,
    probabilistic_sharpe,
    returns_from_equity,
    sharpe,
    skewness,
    sortino,
    win_rate,
)
from .trials import global_trial_n, scan_trials, trial_counts


def _bot_metrics(bot_id: str, db_path: Path, initial_capital: float, n_trial: int,
                 n_strategies: int, n_trials_global: int) -> dict[str, Any]:
    row: dict[str, Any] = {"bot": bot_id}
    if not db_path.exists():
        row["grade"] = "no_data"
        return row
    series = load_equity_series(db_path)
    fills = load_realised_trades(db_path)
    pnls = round_trip_pnls(fills)
    rets = returns_from_equity([eq for _, eq in series])
    start_eq, end_eq, n_snaps = first_equity_or_initial(db_path, initial_capital)
    days = 0.0
    if len(series) >= 2:
        days = (series[-1][0] - series[0][0]) / 86400.0
    ret_pct = (end_eq / start_eq - 1.0) * 100.0 if start_eq > 0 else 0.0
    fees = sum(float(f.get("fee") or 0) for f in fills)
    gross = sum(abs(float(f.get("realised_pnl") or 0)) for f in fills)
    cost_drag = (fees / gross * 100.0) if gross > 0 else 0.0

    row.update({
        "days": round(days, 2),
        "n_snaps": n_snaps,
        "n_trades": len(pnls),
        "n_trial": n_trial,
        "return_pct": round(ret_pct, 3),
        "maxdd_pct": round(max_drawdown_pct([eq for _, eq in series]), 3) if series else 0.0,
        "sharpe": round(sharpe(rets), 4),
        "sortino": round(sortino(rets), 4),
        "calmar": round(calmar([eq for _, eq in series]), 4) if series else 0.0,
        "win_rate": round(win_rate(pnls) * 100.0, 2),
        "profit_factor": (None if profit_factor(pnls) == float("inf") else
                          round(profit_factor(pnls), 4)) if pnls else 0.0,
        "expectancy": round(expectancy(pnls), 4),
        "skew": round(skewness(rets), 4),
        "kurt": round(kurtosis(rets), 4),
        "psr": round(probabilistic_sharpe(rets), 4),
        # 家族规模用 max(N_global, n_strategies)，避免 trials×bots 双重惩罚
        "dsr": round(deflated_sharpe(rets, n_trials=max(n_trials_global, n_strategies),
                                    n_strategies=1), 4),
        "oos_30d_sharpe": round(oos_window_sharpe(series, 30), 4),
        "cost_drag_pct": round(cost_drag, 3),
        "end_equity": round(end_eq, 2),
        "low_n": n_snaps < 14 or len(rets) < 14,
    })

    # grade：样本量徽章
    if n_snaps < 2 or days < 0.05:
        row["grade"] = "no_data" if n_snaps == 0 else "explore"
    elif len(pnls) < 30 or days < 7:
        row["grade"] = "explore"
    elif len(pnls) < 100:
        row["grade"] = "watch"
    else:
        row["grade"] = "cand" if row["dsr"] > 0 else "watch"
    return row


def build_scoreboard(root: Path, run_trial_scan: bool = True) -> dict[str, Any]:
    root = Path(root)
    if run_trial_scan:
        try:
            scan_trials(root)
        except Exception:  # noqa: BLE001
            pass
    counts = trial_counts(root)
    n_global = global_trial_n(root)

    rows = []
    bots_dir = root / "config" / "bots"
    bot_ids = []
    initials = {}
    for yml in sorted(bots_dir.glob("*.yaml")):
        if yml.name.startswith("_"):
            continue
        try:
            import yaml
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        bid = str(data.get("bot_id") or yml.stem)
        if str(data.get("env") or "").lower() != "paper":
            continue
        bot_ids.append(bid)
        initials[bid] = float((data.get("paper") or {}).get("initial_capital") or 10000)

    n_strat = max(1, len(bot_ids))
    for bid in bot_ids:
        db = root / "data" / "bots" / bid / "paper" / "account.db"
        rows.append(_bot_metrics(
            bid, db, initials.get(bid, 10000.0),
            n_trial=counts.get(bid, 0) or 1,
            n_strategies=n_strat,
            n_trials_global=n_global,
        ))

    def sort_key(r: dict[str, Any]):
        if r.get("grade") == "no_data":
            return (2, 0.0, 0.0)
        days = float(r.get("days") or 0)
        # 样本极短时 OOS 优先；否则 DSR 主序
        if days < 3:
            return (0, -float(r.get("oos_30d_sharpe") or 0), -float(r.get("dsr") or 0))
        return (0, -float(r.get("dsr") or 0), -float(r.get("oos_30d_sharpe") or 0))

    rows.sort(key=sort_key)
    return {
        "generated_at": int(time.time()),
        "n_strategies": n_strat,
        "n_trials_global": n_global,
        "rows": rows,
    }


def format_table(board: dict[str, Any]) -> str:
    keys = ["bot", "days", "n_trades", "n_trial", "return_pct", "maxdd_pct",
            "sharpe", "dsr", "oos_30d_sharpe", "calmar", "profit_factor", "win_rate", "grade"]
    header = ["bot", "days", "n_trd", "N", "ret%", "dd%", "sharpe", "dsr",
              "oos30", "calmar", "pf", "win%", "grade"]
    lines = [" | ".join(header), "-|-|-|-|-|-|-|-|-|-|-|-|-"]
    for r in board.get("rows") or []:
        cells = []
        for k in keys:
            v = r.get(k)
            if v is None:
                v = "-"
            elif isinstance(v, float):
                v = f"{v:.4g}"
            cells.append(str(v))
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def write_scoreboard(root: Path, board: Optional[dict] = None) -> Path:
    root = Path(root)
    board = board or build_scoreboard(root)
    out = root / "data" / "metrics" / "scoreboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
