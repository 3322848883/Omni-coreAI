#!/usr/bin/env python3
"""测试服务器飞书通知是否可用。"""
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.monitoring.notify import (  # noqa: E402
    build_notifier,
    notify_process_event,
    should_notify,
)

print("=== 配置检测 ===")
print("  should_notify(live) =", should_notify(ROOT, "live"))
print("  should_notify(paper) =", should_notify(ROOT, "paper"))

n = build_notifier(ROOT)
print("  已注册渠道:", [getattr(c, "name", type(c).__name__) for c in getattr(n, "channels", [])])

print()
print("=== 发送测试通知 ===")
ok = notify_process_event(
    root=ROOT, kind="info",
    title="OmniAlpha 通知测试",
    fields=[("来源", "服务器部署自检"), ("bot", "brooks-btc"), ("结果", "通知链路正常")],
)
print("  发送结果:", ok)
