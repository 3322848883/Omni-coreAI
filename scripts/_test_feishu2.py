#!/usr/bin/env python3
"""复核服务器飞书通知（正确属性名 + 实际发送）。"""
import sys
from pathlib import Path

ROOT = Path("/opt/gate-signal-bot")
sys.path.insert(0, str(ROOT))

from gate_bot.monitoring.notify import build_notifier, notify_process_event  # noqa: E402

n = build_notifier(ROOT)
print("渠道数:", len(n.channel_names))
print("渠道名:", n.channel_names)
print("has_channel:", n.has_channel)
print()

print("=== 发送测试卡片 ===")
ok = notify_process_event(
    root=ROOT, kind="info",
    title="✅ 服务器通知链路测试",
    fields=[
        ("来源", "gate-signal-bot 部署自检"),
        ("bot", "brooks-btc"),
        ("env", "live"),
        ("结论", "飞书通知已启用"),
    ],
)
print("send_card 返回:", ok)
print()
print("→ 请检查飞书是否收到「✅ 服务器通知链路测试」卡片")
