"""告警通知渠道：可扩展的插件式通知系统。

已支持：飞书、Telegram、日志
可扩展：钉钉、Slack、邮件、企业微信...

配置示例：
  notifier = AlertNotifier()
  notifier.register(FeishuChannel(webhook="https://..."))
  notifier.register(TelegramChannel(token="...", chat_id="..."))
  notifier.register(DingTalkChannel(webhook="https://..."))  # 未来扩展
  notifier.send("alert text")
"""
from __future__ import annotations

import json
import logging
import urllib.request
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger(__name__)


# ── 抽象基类：所有通知渠道继承此类 ──────────────────────
class NotificationChannel(ABC):
    """通知渠道基类。新增渠道只需继承并实现 send()。"""

    @abstractmethod
    def send(self, text: str) -> bool:
        """发送消息。返回是否成功。"""
        ...

    @property
    def name(self) -> str:
        return self.__class__.__name__


# ── 飞书 ───────────────────────────────────────────
class FeishuChannel(NotificationChannel):
    """飞书通知：支持 webhook（群机器人）和 app（应用机器人）。"""

    def __init__(self, webhook: Optional[str] = None,
                 app_id: Optional[str] = None,
                 app_secret: Optional[str] = None,
                 user_open_id: Optional[str] = None):
        self.webhook = webhook
        self.app_id = app_id
        self.app_secret = app_secret
        self.user_open_id = user_open_id

    def send(self, text: str) -> bool:
        if self.webhook:
            return self._send_webhook(text)
        if self.app_id and self.app_secret and self.user_open_id:
            return self._send_app(text)
        log.warning("feishu: no webhook or app credentials")
        return False

    def _send_webhook(self, text: str) -> bool:
        try:
            data = json.dumps({"msg_type": "text", "content": {"text": f"[gate-bot] {text}"}}).encode()
            req = urllib.request.Request(self.webhook, data=data,
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()).get("code") == 0
        except Exception as e:
            log.warning("feishu webhook error: %s", e)
            return False

    def _send_app(self, text: str) -> bool:
        try:
            token = self._get_token()
            if not token:
                return False
            msg = json.dumps({
                "receive_id": self.user_open_id,
                "msg_type": "text",
                "content": json.dumps({"text": f"[gate-bot] {text}"}),
            }).encode()
            req = urllib.request.Request(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                data=msg,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()).get("code") == 0
        except Exception as e:
            log.warning("feishu app error: %s", e)
            return False

    def _get_token(self) -> Optional[str]:
        try:
            data = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
            req = urllib.request.Request(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()).get("tenant_access_token")
        except Exception as e:
            log.warning("feishu token error: %s", e)
            return None


# ── Telegram ───────────────────────────────────────
class TelegramChannel(NotificationChannel):
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, text: str) -> bool:
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = json.dumps({"chat_id": self.chat_id,
                               "text": f"[gate-bot] {text}"}).encode()
            req = urllib.request.Request(url, data=data,
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()).get("ok", False)
        except Exception as e:
            log.warning("telegram error: %s", e)
            return False


# ── 钉钉（未来扩展示例）─────────────────────────────
class DingTalkChannel(NotificationChannel):
    """钉钉群机器人（预留接口）。"""

    def __init__(self, webhook: str, secret: Optional[str] = None):
        self.webhook = webhook
        self.secret = secret

    def send(self, text: str) -> bool:
        try:
            data = json.dumps({"msgtype": "text", "text": {"content": f"[gate-bot] {text}"}}).encode()
            req = urllib.request.Request(self.webhook, data=data,
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()).get("errcode") == 0
        except Exception as e:
            log.warning("dingtalk error: %s", e)
            return False


# ── 主类：插件式注册 ────────────────────────────────
class AlertNotifier:
    """告警通知管理器：注册多个渠道，统一发送。"""

    def __init__(self):
        self._channels: list[NotificationChannel] = []

    def register(self, channel: NotificationChannel) -> "AlertNotifier":
        """注册通知渠道（链式调用）。"""
        self._channels.append(channel)
        return self

    @property
    def has_channel(self) -> bool:
        return len(self._channels) > 0

    @property
    def channel_names(self) -> list[str]:
        return [c.name for c in self._channels]

    def send(self, text: str) -> bool:
        """发送到所有已注册渠道。返回是否有任一成功。"""
        if not self._channels:
            log.warning("alert (no channel): %s", text)
            return False
        ok = False
        for ch in self._channels:
            try:
                ok = ch.send(text) or ok
            except Exception as e:
                log.warning("channel %s error: %s", ch.name, e)
        return ok
