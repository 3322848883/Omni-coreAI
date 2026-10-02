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
            data = json.dumps({"msg_type": "text", "content": {"text": f"[omnialpha] {text}"}}).encode()
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
                "content": json.dumps({"text": f"[omnialpha] {text}"}),
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
                               "text": f"[omnialpha] {text}"}).encode()
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
            data = json.dumps({"msgtype": "text", "text": {"content": f"[omnialpha] {text}"}}).encode()
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




def should_notify(root: Path = None, env: str = "live") -> bool:
    """通知开关：实盘默认开、模拟盘默认关。

    config/alerts.yaml:
      notify:
        live: true
        paper: false
    """
    try:
        cfg = _load_alerts_yaml(root or Path.cwd())
        sw = (cfg.get("notify") or {}) if isinstance(cfg, dict) else {}
        env = (env or "live").lower()
        if env == "paper":
            return bool(sw.get("paper", False))
        return bool(sw.get("live", True))
    except Exception:  # noqa: BLE001
        # 读不到配置时：实盘开、模拟盘关（安全默认）
        return (env or "live").lower() != "paper"

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
            px = _entry_price(detail, s)
            sz, unit = _resolve_size(detail, s)
            lines.append(f"{mark} 开仓 {sym} {side} @{px} size={sz}{unit}")
        elif action in _CLOSE_ACTIONS:
            px = _entry_price(detail, s)
            pnl = _resolve_pnl(detail, s)
            pnl_val = 0.0
            try:
                pnl_val = float(pnl) if pnl is not None else 0.0
            except (TypeError, ValueError):
                pnl_val = 0.0
            label = "止盈" if pnl_val > 0 else ("止损" if pnl_val < 0 else "平仓")
            icon = "✅" if pnl_val > 0 else ("🔴" if pnl_val < 0 else "📉")
            lines.append(f"{mark} {icon} {label} {sym} @{px} pnl={_fmt_num(pnl)}")
        elif action in _REDUCE_ACTIONS:
            px = _entry_price(detail, s)
            sz, unit = _resolve_size(detail, s)
            lines.append(f"{mark} 减仓 {sym} @{px} size={sz}{unit}")
        elif action == "modify_tp_sl":
            tp = _resolve_tp(detail, s)
            sl = _resolve_sl(detail, s)
            lines.append(f"{mark} 改保护 {sym} TP={_fmt_num(tp)} SL={_fmt_num(sl)}")
    return lines


def _fmt_num(v, digits: int = 2) -> str:
    """数字格式化：去掉浮点噪声，空值显示 —。"""
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
        if f == int(f):
            return str(int(f))
        return f"{f:.{digits}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v) or "—"


def _entry_price(detail: dict, s: dict) -> str:
    """入场价：detail.entry_price → order.fill_price/price → detail.price。"""
    for src in (
        detail.get("entry_price"),
        (detail.get("order") or {}).get("fill_price"),
        (detail.get("order") or {}).get("price"),
        detail.get("price"),
        detail.get("avg_price"),
        s.get("price"),
    ):
        if src is not None and src != "" and str(src) != "0":
            return _fmt_num(src, 1)
    return "—"


def _first_val(*vals):
    """取第一个「有内容」的值（None / 空串 / 0 都算无）。"""
    for v in vals:
        if v is None or v == "" or str(v) == "0":
            continue
        return v
    return None


def _leg_price(legs):
    """tp_orders / sl_orders 形如 [{trigger_price|price|trigger}]，取第一腿。"""
    if not isinstance(legs, (list, tuple)):
        return None
    for leg in legs:
        if isinstance(leg, dict):
            v = _first_val(leg.get("trigger_price"), leg.get("price"), leg.get("trigger"))
            if v is not None:
                return v
    return None


def _resolve_tp(detail: dict, s: dict):
    """止盈价：detail.tp → step.tp → tp_placed.price → tp_orders[].trigger_price。

    不同 action 的 detail 形状不同（open_* 用 detail.tp；modify_tp_sl 用
    detail.tp_placed.price；stop_entry_* 只给 tp_orders[]），所以必须逐层兜底。
    """
    return _first_val(
        detail.get("tp"),
        s.get("tp"),
        (detail.get("tp_placed") or {}).get("price"),
        _leg_price(detail.get("tp_orders")),
    )


def _resolve_sl(detail: dict, s: dict):
    """止损价：detail.sl → step.sl → sl_placed.price → sl_orders[].trigger_price。"""
    return _first_val(
        detail.get("sl"),
        s.get("sl"),
        (detail.get("sl_placed") or {}).get("price"),
        _leg_price(detail.get("sl_orders")),
    )


def _resolve_pnl(detail: dict, s: dict):
    """已实现盈亏：0 也是有效值（保本平仓），所以不走 _first_val。"""
    for src in (
        detail.get("realized_pnl"),
        detail.get("pnl"),
        detail.get("pnl_usd"),
        (detail.get("order") or {}).get("pnl"),
        s.get("pnl"),
    ):
        if src is not None and src != "":
            return src
    return None


def _resolve_size(detail: dict, s: dict):
    """仓位数量 → (显示值, 单位)。优先 USD 名义；拿不到就退化成合约张数。"""
    usd = _first_val(detail.get("size_usd"))
    if usd is not None:
        return _fmt_num(usd), "USDT"
    order = detail.get("order") or {}
    qty = _first_val(
        order.get("size"),
        order.get("filled_size"),
        detail.get("size"),
        detail.get("contracts"),
        s.get("size"),
    )
    if qty is None:
        return "—", ""
    try:
        q = abs(float(qty))
    except (TypeError, ValueError):
        return _fmt_num(qty), "张"
    return (_fmt_num(q, 0) if q == int(q) else _fmt_num(q)), "张"


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
            px = _entry_price(detail, s)
            sz, unit = _resolve_size(detail, s)
            sl = _resolve_sl(detail, s)
            tp = _resolve_tp(detail, s)
            color = "green"
            title = f"📈 开仓告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("方向", f"开{side}"), ("入场价", px),
                ("仓位", f"{sz} {unit}".strip()), ("止损", _fmt_num(sl)),
                ("止盈", _fmt_num(tp)),
            ]
        elif action in _CLOSE_ACTIONS:
            px = _entry_price(detail, s)
            pnl = _resolve_pnl(detail, s)
            pnl_val = 0
            try:
                pnl_val = float(pnl) if pnl is not None else 0
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
                ("平仓价", px), ("盈亏", f"{_fmt_num(pnl)} USDT"),
            ]
        elif action in _REDUCE_ACTIONS:
            px = _entry_price(detail, s)
            sz, unit = _resolve_size(detail, s)
            color = "blue"
            title = f"📊 减仓告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("减仓价", px), ("减仓量", f"{sz} {unit}".strip()),
            ]
        elif action == "modify_tp_sl":
            tp = _resolve_tp(detail, s)
            sl = _resolve_sl(detail, s)
            color = "blue"
            title = f"✏️ 改单告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("止盈", _fmt_num(tp)), ("止损", _fmt_num(sl)),
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


def notify_trade_events(bot_id: str, steps: list, root: Path = None, env: str = "live") -> bool:
    """成交事件推送（飞书卡片）。env=paper 且 notify.paper=false 时静默跳过。"""
    if not should_notify(root, env):
        return False
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
