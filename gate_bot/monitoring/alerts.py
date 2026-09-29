"""告警落盘：把关键异常写入 state/alerts.json，供体检脚本/人工读取。

触发类型（P0.4）：
  - equity_deviation  权益偏离 >10%
  - dup_fill          同一 order_id 成交入账重复
  - orphan_protector  平仓后遗留 reduce-only SL/TP

数据文件：data/bots/<id>/state/alerts.json
结构：{"alerts": [{ts, type, detail, ...}, ...]}  最多保留 200 条。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

MAX_ALERTS = 200

# 告警类型常量
TYPE_EQUITY_DEVIATION = "equity_deviation"
TYPE_DUP_FILL = "dup_fill"
TYPE_ORPHAN = "orphan_protector"


class AlertStore:
    def __init__(self, root: Path, bot_id: str):
        self.root = Path(root)
        self.bot_id = bot_id
        self.path = self.root / "data" / "bots" / bot_id / "state" / "alerts.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── 读写 ──────────────────────────────────────
    def _load(self) -> list[dict]:
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8"))
            return list(rec.get("alerts") or [])
        except Exception:  # noqa: BLE001
            return []

    def _save(self, alerts: list[dict]) -> None:
        rec = {"alerts": alerts[-MAX_ALERTS:], "updated": int(time.time())}
        self.path.write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 写告警 ────────────────────────────────────
    def raise_alert(self, type_: str, detail: str, **extra: Any) -> dict:
        rec = {"ts": int(time.time()), "type": type_, "detail": detail, **extra}
        alerts = self._load()
        alerts.append(rec)
        self._save(alerts)
        return rec

    def equity_deviation(self, start: float, current: float,
                         threshold_pct: float = 10.0) -> Optional[dict]:
        """权益相对日初偏离超阈值时告警。"""
        if start <= 0:
            return None
        dev = (current - start) / start * 100.0
        if abs(dev) < threshold_pct:
            return None
        return self.raise_alert(
            TYPE_EQUITY_DEVIATION,
            f"equity {current:.2f} vs start {start:.2f} ({dev:+.1f}%)",
            start=start, current=current, deviation_pct=round(dev, 2),
            threshold_pct=threshold_pct,
        )

    def dup_fill(self, order_id: str, size: float, price: float) -> dict:
        return self.raise_alert(
            TYPE_DUP_FILL,
            f"duplicate fill order_id={order_id} size={size} price={price}",
            order_id=order_id, size=size, price=price,
        )

    def orphan(self, symbol: str, count: int, order_ids: Optional[list] = None) -> dict:
        return self.raise_alert(
            TYPE_ORPHAN,
            f"orphan protectors symbol={symbol} count={count}",
            symbol=symbol, count=count, order_ids=order_ids or [],
        )

    # ── 查询 ──────────────────────────────────────
    def list(self, type_: Optional[str] = None) -> list[dict]:
        alerts = self._load()
        if type_ is not None:
            alerts = [a for a in alerts if a.get("type") == type_]
        return alerts

    def clear(self) -> None:
        self._save([])


def read_alerts(root: Path, bot_id: str, type_: Optional[str] = None) -> list[dict]:
    """体检脚本入口。"""
    return AlertStore(root, bot_id).list(type_)
