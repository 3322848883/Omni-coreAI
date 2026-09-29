"""告警通知渠道：飞书群机器人 + Telegram Bot。

配置（config/alerts.yaml 或环境变量）：
  feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
  telegram_bot_token: "123456:ABC..."
  telegram_chat_id: "123456789"

用法：
  notifier = AlertNotifier(feishu_webhook="https://...")
  notifier.send("alert: llm_latency 150s")
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)


class AlertNotifier:
    """飞书 + Telegram 告警通知。"""

    def __init__(self,
                 feishu_webhook: Optional[str] = None,
                 telegram_bot_token: Optional[str] = None,
                 telegram_chat_id: Optional[str] = None):
        self.feishu_webhook = feishu_webhook
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id

    @property
    def has_channel(self) -> bool:
        return bool(self.feishu_webhook or (self.telegram_bot_token and self.telegram_chat_id))

    def send(self, text: str) -> bool:
        """发送告警到所有已配置渠道。返回是否有任一成功。"""
        if not self.has_channel:
            log.warning("alert (no channel): %s", text)
            return False
        ok = False
        if self.feishu_webhook:
            ok = self._send_feishu(text) or ok
        if self.telegram_bot_token and self.telegram_chat_id:
            ok = self._send_telegram(text) or ok
        return ok

    def _send_feishu(self, text: str) -> bool:
        try:
            data = json.dumps({
                "msg_type": "text",
                "content": {"text": f"[gate-bot] {text}"},
            }).encode("utf-8")
            req = urllib.request.Request(
                self.feishu_webhook,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                if body.get("code") == 0:
                    log.info("feishu alert sent")
                    return True
                log.warning("feishu alert failed: %s", body)
                return False
        except Exception as e:  # noqa: BLE001
            log.warning("feishu alert error: %s", e)
            return False

    def _send_telegram(self, text: str) -> bool:
        try:
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            data = json.dumps({
                "chat_id": self.telegram_chat_id,
                "text": f"[gate-bot] {text}",
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                if body.get("ok"):
                    log.info("telegram alert sent")
                    return True
                log.warning("telegram alert failed: %s", body)
                return False
        except Exception as e:  # noqa: BLE001
            log.warning("telegram alert error: %s", e)
            return False
