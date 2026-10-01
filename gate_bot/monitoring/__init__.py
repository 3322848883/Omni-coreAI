"""gate_bot.monitoring — 策略衰减检测 + 健康监控告警 + 告警落盘 + 可扩展通知渠道。"""
from .alerts import (
    TYPE_DUP_FILL,
    TYPE_EQUITY_DEVIATION,
    TYPE_ORPHAN,
    TYPE_PLAN_FAIL,
    AlertStore,
    read_alerts,
)
from .decay import DecayDetector
from .health import HealthMonitor
from .notify import (
    AlertNotifier,
    DingTalkChannel,
    FeishuChannel,
    NotificationChannel,
    TelegramChannel,
    build_notifier,
    format_process_card,
    format_trade_card,
    should_notify,
    format_trade_steps,
    notify_process_event,
    notify_trade_events,
)

__all__ = [
    "DecayDetector", "HealthMonitor", "AlertNotifier",
    "NotificationChannel", "FeishuChannel", "TelegramChannel", "DingTalkChannel",
    "AlertStore", "read_alerts",
    "TYPE_EQUITY_DEVIATION", "TYPE_DUP_FILL", "TYPE_ORPHAN", "TYPE_PLAN_FAIL",
    "build_notifier", "format_trade_steps", "format_trade_card", "notify_trade_events", "should_notify",
    "format_process_card", "notify_process_event",
]
