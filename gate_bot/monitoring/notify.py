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
from pathlib import Path
import urllib.request
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger(__name__)


# ── 抽象基类：所有通知渠道继承此类 ──────────────────────
class NotificationChannel(ABC):
    """通知渠道基类。新增渠道只需继承并实现 send()。"""

    def send_card(self, card: dict) -> bool:
        """发送卡片消息（默认降级为文本）。"""
        return self.send(str(card))

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

    def send_card(self, card: dict) -> bool:
        """发送飞书卡片消息。"""
        try:
            if self.webhook:
                data = json.dumps({"msg_type": "interactive", "card": card}).encode()
                req = urllib.request.Request(self.webhook, data=data,
                                            headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read()).get("code") == 0
            if self.app_id and self.app_secret and self.user_open_id:
                token = self._get_token()
                if not token:
                    return False
                msg = json.dumps({
                    "receive_id": self.user_open_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                }).encode()
                req = urllib.request.Request(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                    data=msg,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read()).get("code") == 0
        except Exception as e:
            log.warning("feishu card error: %s", e)
            return False
        return False

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

    def send_card(self, card: dict) -> bool:
        """发送飞书卡片到所有已注册渠道。"""
        if not self._channels:
            return False
        ok = False
        for ch in self._channels:
            try:
                if hasattr(ch, "send_card"):
                    ok = ch.send_card(card) or ok
                else:
                    ok = ch.send(str(card)) or ok
            except Exception as e:
                log.warning("channel %s card error: %s", ch.name, e)
        return ok

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


# ── 工厂：从环境变量构建（飞书/Telegram） ────────────
# ── 成交事件动作分类 ──────────────────────────────
_OPEN_ACTIONS = {"open_long", "open_short", "add_long", "add_short",
                 "stop_entry_long", "stop_entry_short", "stop_long", "stop_short"}
_CLOSE_ACTIONS = {"close", "close_long", "close_short", "close_all", "flatten"}
_REDUCE_ACTIONS = {"reduce", "reduce_long", "reduce_short"}
_MANAGE_ACTIONS = {"modify_tp_sl", "hold", "cancel_all", "cancel_price_all"}


def _load_alerts_yaml(root: Path) -> dict:
    """读 config/alerts.yaml（含密钥，不入 git）。"""
    try:
        import yaml
        p = Path(root) / "config" / "alerts.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("load alerts.yaml failed: %s", e)
    return {}


def build_notifier(root: Path = None) -> AlertNotifier:
    """注册通知渠道：优先环境变量，其次 config/alerts.yaml。"""
    import os

    n = AlertNotifier()
    webhook = os.environ.get("FEISHU_WEBHOOK")
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    user = os.environ.get("FEISHU_USER_OPEN_ID")
    tg_token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not webhook and not (app_id and app_secret):
        cfg = _load_alerts_yaml(root or Path.cwd())
        feishu = cfg.get("feishu") or {}
        webhook = webhook or feishu.get("webhook")
        app_id = app_id or feishu.get("app_id")
        app_secret = app_secret or feishu.get("app_secret")
        user = user or feishu.get("user_open_id")
        tg = cfg.get("telegram") or {}
        tg_token = tg_token or tg.get("bot_token")
        tg_chat = tg_chat or tg.get("chat_id")
    if webhook:
        n.register(FeishuChannel(webhook=webhook))
    elif app_id and app_secret and user:
        n.register(FeishuChannel(app_id=app_id, app_secret=app_secret, user_open_id=user))
    if tg_token and tg_chat:
        n.register(TelegramChannel(bot_token=tg_token, chat_id=tg_chat))
    return n
def format_trade_steps(bot_id: str, steps: list) -> list[str]:
    """把 ExecReport.steps 里值得推送的成交/保护事件格式化成文本行。

    只推：开仓、平仓、减仓、modify_tp_sl；hold/cancel 静默。
    """
    lines: list[str] = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action") or "")
        if action in _MANAGE_ACTIONS and action != "modify_tp_sl":
            continue
        ok = bool(s.get("ok"))
        sym = s.get("symbol") or ""
        detail = s.get("detail") or {}
        mark = "✓" if ok else "✗"
        if action in _OPEN_ACTIONS:
            side = "多" if "long" in action else "空"
            px = detail.get("price") or detail.get("avg_price") or ""
            sz = detail.get("size_usd") or detail.get("filled_size") or ""
            lines.append(f"{mark} 开仓 {sym} {side} @{px} size={sz}")
        elif action in _CLOSE_ACTIONS:
            px = detail.get("price") or detail.get("avg_price") or ""
            pnl = detail.get("realized_pnl") or detail.get("pnl") or detail.get("pnl_usd") or ""
            pnl_val = float(pnl) if pnl else 0
            label = "止盈" if pnl_val > 0 else ("止损" if pnl_val < 0 else "平仓")
            icon = "✅" if pnl_val > 0 else ("🔴" if pnl_val < 0 else "📉")
            lines.append(f"{mark} {icon} {label} {sym} @{px} pnl={pnl}")
        elif action in _REDUCE_ACTIONS:
            px = detail.get("price") or ""
            sz = detail.get("size") or detail.get("size_usd") or ""
            lines.append(f"{mark} 减仓 {sym} @{px} size={sz}")
        elif action == "modify_tp_sl":
            tp = detail.get("tp") or s.get("tp") or ""
            sl = detail.get("sl") or s.get("sl") or ""
            lines.append(f"{mark} 改保护 {sym} TP={tp} SL={sl}")
    return lines


def format_trade_card(bot_id: str, steps: list) -> list[dict]:
    """把成交事件格式化成飞书卡片元素（彩色标题+字段布局）。"""
    cards = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action") or "")
        if action in _MANAGE_ACTIONS and action != "modify_tp_sl":
            continue
        ok = bool(s.get("ok"))
        sym = s.get("symbol") or ""
        detail = s.get("detail") or {}

        if action in _OPEN_ACTIONS:
            side = "多" if "long" in action else "空"
            px = detail.get("price") or detail.get("avg_price") or ""
            sz = detail.get("size_usd") or detail.get("filled_size") or ""
            sl = detail.get("sl") or ""
            tp = detail.get("tp") or ""
            color = "green"
            title = f"📈 开仓告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("方向", f"开{side}"), ("入场价", str(px)),
                ("仓位", f"{sz} USDT"), ("止损", str(sl)),
                ("止盈", str(tp)),
            ]
        elif action in _CLOSE_ACTIONS:
            px = detail.get("price") or detail.get("avg_price") or ""
            pnl = detail.get("realized_pnl") or detail.get("pnl") or detail.get("pnl_usd") or ""
            pnl_val = 0
            try:
                pnl_val = float(pnl) if pnl else 0
            except (TypeError, ValueError):
                pass
            if pnl_val > 0:
                color, icon, label = "green", "\u2705", "止盈"
            elif pnl_val < 0:
                color, icon, label = "red", "\U0001f534", "止损"
            else:
                color, icon, label = "blue", "\U0001f4c9", "平仓"
            title = f"{icon} {label}告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("平仓价", str(px)), ("盈亏", f"{pnl} USDT"),
            ]
        elif action in _REDUCE_ACTIONS:
            px = detail.get("price") or ""
            sz = detail.get("size") or detail.get("size_usd") or ""
            color = "blue"
            title = f"📊 减仓告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("减仓价", str(px)), ("减仓量", f"{sz} USDT"),
            ]
        elif action == "modify_tp_sl":
            tp = detail.get("tp") or s.get("tp") or ""
            sl = detail.get("sl") or s.get("sl") or ""
            color = "blue"
            title = f"✏️ 改单告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("止盈", str(tp)), ("止损", str(sl)),
            ]
        else:
            continue

        # 构建飞书卡片
        field_elements = []
        for i in range(0, len(fields), 2):
            pair = fields[i:i+2]
            field_elements.append({
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**{k}:** {v}"}}
                    for k, v in pair
                ],
            })
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
            "elements": field_elements,
        }
        cards.append(card)
    return cards


def notify_trade_events(bot_id: str, steps: list, root: Path = None) -> bool:
    """成交事件推送（飞书卡片，无渠道时静默返回 False）。"""
    cards = format_trade_card(bot_id, steps)
    if not cards:
        return False
    try:
        n = build_notifier(root=root)
        if not n.has_channel:
            return False
        ok = False
        for card in cards:
            ok = n.send_card(card) or ok
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("trade notify error: %s", e)
        return False

# ── 进程/看门狗事件卡片（与成交卡片同风格） ─────────
def format_process_card(kind: str, title: str, fields: Optional[list] = None, color: str = "blue") -> dict:
    """看门狗/进程事件飞书卡片。kind: restart | storm | start | stop | info。"""
    icon = {
        "restart": "↻", "storm": "⛔", "start": "🐕",
        "stop": "🛑", "info": "ℹ️",
    }.get(kind, "ℹ️")
    field_elements = []
    for i in range(0, len(fields or []), 2):
        pair = (fields or [])[i:i + 2]
        field_elements.append({
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**{k}:** {v}"}}
                for k, v in pair
            ],
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"{icon} {title}"}, "template": color},
        "elements": field_elements or [{"tag": "div", "text": {"tag": "lark_md", "content": title}}],
    }


def notify_process_event(root: Path = None, kind: str = "info", title: str = "",
                         fields: Optional[list] = None, color: str = "blue") -> bool:
    """进程事件推送（飞书卡片）。"""
    try:
        n = build_notifier(root=root)
        if not n.has_channel:
            return False
        card = format_process_card(kind, title, fields, color)
        return n.send_card(card)
    except Exception as e:  # noqa: BLE001
        log.warning("process notify error: %s", e)
        return False
