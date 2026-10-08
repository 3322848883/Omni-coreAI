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
import os
import re
import time
from datetime import datetime, timedelta, timezone
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
        if detail.get("noop"):
            # 良性 no-op（如无持仓的 modify_tp_sl）：什么都没发生。
            # 推送「改单告警 止盈:— 止损:—」只会让人以为失败了 —— 静默。
            continue
        mark = "✓" if ok else "✗"
        tstamp = _step_time(detail, s)
        if action in _OPEN_ACTIONS:
            side = "多" if "long" in action else "空"
            px = _entry_price(detail, s)
            sz, unit = _resolve_size(detail, s)
            sl_txt = _fmt_levels(_exit_levels(detail, s, "sl"))
            tp_txt = _fmt_levels(_exit_levels(detail, s, "tp"))
            # 挂单 ≠ 开仓：GTC 限价单下出去时仓位还没建立
            verb = "挂单" if _is_resting(detail, s) else "开仓"
            lines.append(f"{mark} {verb} {sym} {side} @{px} size={sz}{unit} "
                         f"SL={sl_txt} TP={tp_txt}  [{tstamp}]")
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
            lines.append(f"{mark} {icon} {label} {sym} @{px} pnl={_fmt_num(pnl)}  [{tstamp}]")
        elif action in _REDUCE_ACTIONS:
            px = _entry_price(detail, s)
            sz, unit = _resolve_size(detail, s)
            lines.append(f"{mark} 减仓 {sym} @{px} size={sz}{unit}  [{tstamp}]")
        elif action == "modify_tp_sl":
            tp_txt = _fmt_levels(_exit_levels(detail, s, "tp"))
            sl_txt = _fmt_levels(_exit_levels(detail, s, "sl"))
            lines.append(f"{mark} 改保护 {sym} TP={tp_txt} SL={sl_txt}  [{tstamp}]")
    return lines


# 通知时间统一用北京时间（用户要求）。
# 可用环境变量 `NOTIFY_TZ` 覆盖（如 "UTC" / "America/New_York"）；
# 取不到时区数据（Windows 缺 tzdata / 名字非法）时退回固定 UTC+8。
DEFAULT_NOTIFY_TZ = "Asia/Shanghai"
_TZ_FALLBACK = timezone(timedelta(hours=8))
_TZ_CACHE: dict = {}


def _notify_tzinfo():
    """通知用哪个时区。

    优先 env `NOTIFY_TZ`：既接受 IANA 名（"Asia/Shanghai"，需 tzdata），
    也接受固定偏移写法（"+08:00" / "-05:00"，不依赖 tzdata）。
    都拿不到就退回固定 UTC+8。
    """
    if "tz" not in _TZ_CACHE:
        name = (os.environ.get("NOTIFY_TZ") or DEFAULT_NOTIFY_TZ).strip()
        tz = _TZ_FALLBACK
        if name:
            m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})", name)
            if m:
                sign = 1 if m.group(1) == "+" else -1
                tz = timezone(sign * timedelta(hours=int(m.group(2)),
                                               minutes=int(m.group(3))))
            else:
                try:
                    from zoneinfo import ZoneInfo

                    tz = ZoneInfo(name)
                except Exception:  # noqa: BLE001 — 缺 tzdata / 名字非法 → 固定 +8
                    tz = _TZ_FALLBACK
        _TZ_CACHE["tz"] = tz
    return _TZ_CACHE["tz"]


def _fmt_ts(v) -> str:
    """把 Unix 时间戳格式化成**北京时间**（可经 NOTIFY_TZ 覆盖）。

    用**事件发生时间**而不是发送时间：飞书自己会显示送达时间，但两者可能差很远
    （LLM 一轮 30-90 秒、或进程重启后补发）。
    统一北京时间是因为服务器跑 UTC 而读卡片的人在 UTC+8 —— 光看「15:15」会误判成
    下午三点，实际是晚上十一点。
    """
    if v is None or v == "":
        return "—"
    try:
        ts = float(v)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    tz = _notify_tzinfo()
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return "—"
    off = dt.strftime("%z") or ""
    off = f"{off[:3]}:{off[3:]}" if len(off) == 5 else off
    zone = "北京时间" if off == "+08:00" else off
    return dt.strftime("%Y-%m-%d %H:%M:%S") + (f" {zone}" if zone else "")


def _step_time(detail: dict, s: dict) -> str:
    """取该 step 的事件时间：订单 create_time → update_time → 当前时间。"""
    o = detail.get("order") or {}
    for src in (o.get("create_time"), o.get("update_time"),
                detail.get("create_time"), s.get("create_time"), s.get("ts")):
        if src not in (None, "", 0, "0"):
            out = _fmt_ts(src)
            if out != "—":
                return out
    return _fmt_ts(time.time())


def _unwrap_price(v):
    """从 Gate 的触发单规格里取出价格。

    `stop_entry_*` 类的 `detail.sl`/`detail.tp` 可能是**字典**（如
    `{'strategy_type':0,'price_type':1,'price':'86080.0','rule':2,'bbo':'','expiration':0}`），
    直接 str() 会把整段原始代码推到卡片上（线上 2026-10-02 实测）。这里逐层取出 price。
    """
    if isinstance(v, dict):
        for k in ("price", "trigger_price", "trigger", "order_price"):
            inner = v.get(k)
            if inner not in (None, "", 0, "0"):
                return _unwrap_price(inner)
        return None
    return v


def _fmt_num(v, digits: int = 2) -> str:
    """数字格式化：去掉浮点噪声，空值显示 —。**绝不打印原始容器**。"""
    v = _unwrap_price(v)
    if v is None or v == "":
        return "—"
    if isinstance(v, (dict, list, tuple)):
        return "—"  # 兜底：拿不到可读值，也不要把原始结构推给用户
    try:
        f = float(v)
        if f == int(f):
            return str(int(f))
        return f"{f:.{digits}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v) or "—"


def _entry_price(detail: dict, s: dict) -> str:
    """入场价：detail.entry_price → order.fill_price/price → detail.price。

    `stop_entry_*` 的价格在 `order.trigger.price`（触发价），不在 `order.price`
    （那里是 0），所以必须一并兜底。
    """
    o = detail.get("order") or {}
    body = detail.get("body") or {}
    for src in (
        detail.get("entry_price"),
        o.get("fill_price"),
        o.get("avg_price"),
        o.get("price"),
        (o.get("trigger") or {}).get("price"),
        (body.get("trigger") or {}).get("price"),
        (o.get("initial") or {}).get("price"),
        detail.get("price"),
        detail.get("avg_price"),
        s.get("price"),
    ):
        if src is not None and src != "" and str(src) != "0":
            return _fmt_num(src, 1)
    return "—"


def _is_resting(detail: dict, s: dict) -> bool:
    """这笔开仓是**挂在盘口等成交**，还是**已经成交**？

    `open_*` 的 step 只表示「委托下出去了」—— GTC 限价单此时 `status=open`、
    `fill_price=0`，仓位根本还没建立。旧文案一律写「开仓」，线上被误读成
    「已经进场了」（用户反馈：通知里的开仓告警其实只是挂单）。

    判据按可靠性排序：
      1. `filled_size` / `fill_price` / `avg_price` 有值 → 已成交
      2. `status` 为 finished/closed/filled → 已成交；`open` → 还挂着
      3. 其余情况（字段缺失）按「挂着」处理 —— 宁可说轻，不要说成已进场
    """
    o = detail.get("order") or {}
    if _first_val(o.get("filled_size"), o.get("fill_price"), o.get("avg_price")) is not None:
        return False
    st = str(o.get("status") or "").lower()
    if st in ("finished", "closed", "filled"):
        return False
    return True


def _first_val(*vals):
    """取第一个「有内容」的值（None / 空串 / 0 都算无）。"""
    for v in vals:
        if v is None or v == "" or str(v) == "0":
            continue
        return v
    return None


def _exit_levels(detail: dict, s: dict, kind: str) -> list:
    """收集 tp / sl 的**所有**价位 → [(price, size)]，按触发顺序。

    多级止盈（tp/tp2/tp3）会挂成**多腿**条件单，所以必须全部渲染 ——
    只显示第一腿会丢掉其余档位（线上实测 40 个执行里 6 个是多腿）。

    数据来源（各 action 形状不同，逐层兜底）：
      1) `{kind}_orders[]`  ← 实际挂出的多腿（含每腿张数），最可靠
      2) `{kind}_placed.price`  ← modify_tp_sl 的单腿
      3) `detail.tp/tp2/tp3`、`detail.sl`  ← AI 意图（open_* 有）
      4) `step.tp / step.sl`
    """
    out: list = []
    orders = detail.get(f"{kind}_orders")
    if isinstance(orders, list):
        for leg in orders:
            if not isinstance(leg, dict):
                continue
            tr = leg.get("trigger") or {}
            px = _unwrap_price(_first_val(
                tr.get("price"), leg.get("trigger_price"), leg.get("price"),
            ))
            if px is None:
                continue
            out.append((px, (leg.get("initial") or {}).get("size")))
    if out:
        return out
    placed = (detail.get(f"{kind}_placed") or {}).get("price")
    if placed is not None:
        return [(placed, None)]
    if kind == "tp":
        for key in ("tp", "tp2", "tp3"):
            v = _unwrap_price(_first_val(detail.get(key)))
            if v is not None:
                out.append((v, None))
    else:
        v = _unwrap_price(_first_val(detail.get("sl")))
        if v is not None:
            out.append((v, None))
    if out:
        return out
    v = _unwrap_price(_first_val(s.get(kind)))
    return [(v, None)] if v is not None else []


def _fmt_levels(levels: list) -> str:
    """[(price, size)] → `86150 / 86800（8/9 张）`；单档就是 `86150`。"""
    if not levels:
        return "—"
    text = " / ".join(_fmt_num(p) for p, _ in levels)
    if len(levels) > 1:
        sizes = []
        for _, sz in levels:
            try:
                q = abs(float(sz))
                sizes.append(str(int(q)) if q == int(q) else f"{q:g}")
            except (TypeError, ValueError):
                sizes = []
                break
        if sizes:
            text += f"（{'/'.join(sizes)} 张）"
    return text


def _resolve_tp(detail: dict, s: dict):
    """止盈价（首档，兼容旧调用方）。多档请用 `_exit_levels`。"""
    lv = _exit_levels(detail, s, "tp")
    return lv[0][0] if lv else None


def _resolve_sl(detail: dict, s: dict):
    """止损价（首档，兼容旧调用方）。多档请用 `_exit_levels`。"""
    lv = _exit_levels(detail, s, "sl")
    return lv[0][0] if lv else None


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
    """仓位数量 → (显示值, 单位)。优先 USD 名义；拿不到就退化成合约张数。

    `stop_entry_*` 的数量在 `order.initial.size`（`order.size` 不存在），必须兜底。
    """
    usd = _first_val(detail.get("size_usd"))
    if usd is not None:
        return _fmt_num(usd), "USDT"
    order = detail.get("order") or {}
    body = detail.get("body") or {}
    oinit = order.get("initial") or {}
    binit = body.get("initial") or {}
    qty = _first_val(
        order.get("size"),
        order.get("filled_size"),
        oinit.get("size"),
        oinit.get("amount"),
        binit.get("size"),
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
        if detail.get("noop"):
            continue  # 良性 no-op：什么都没发生，不推送（同 format_trade_steps）
        tstamp = _step_time(detail, s)

        if action in _OPEN_ACTIONS:
            side = "多" if "long" in action else "空"
            px = _entry_price(detail, s)
            sz, unit = _resolve_size(detail, s)
            sl_txt = _fmt_levels(_exit_levels(detail, s, "sl"))
            tp_txt = _fmt_levels(_exit_levels(detail, s, "tp"))
            color = "green"
            # 挂单 ≠ 开仓：GTC 限价单下出去时仓位还没建立，标题要如实说。
            # **字段名保持「入场价」不变** —— 它是给消费方看的稳定契约，
            # 只有标题与颜色随「挂着 / 已成交」变化。
            if _is_resting(detail, s):
                color = "blue"
                title = "📋 挂单告警"
            else:
                title = "📈 开仓告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("方向", f"开{side}"), ("入场价", px),
                ("仓位", f"{sz} {unit}".strip()), ("止损", sl_txt),
                ("止盈", tp_txt), ("时间", tstamp),
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
                ("时间", tstamp),
            ]
            # close_all/flatten 的 detail 只有 closed_order_ids（没有 order/价格），
            # 那就报「已平订单数」而不是留一个空的价格字段
            closed_ids = detail.get("closed_order_ids")
            if px == "—" and isinstance(closed_ids, list) and closed_ids:
                fields.append(("已平订单", str(len(closed_ids))))
        elif action in _REDUCE_ACTIONS:
            px = _entry_price(detail, s)
            sz, unit = _resolve_size(detail, s)
            color = "blue"
            title = f"📊 减仓告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("减仓价", px), ("减仓量", f"{sz} {unit}".strip()),
                ("时间", tstamp),
            ]
        elif action == "modify_tp_sl":
            tp_txt = _fmt_levels(_exit_levels(detail, s, "tp"))
            sl_txt = _fmt_levels(_exit_levels(detail, s, "sl"))
            color = "blue"
            title = f"✏️ 改单告警"
            fields = [
                ("Bot", bot_id), ("币种", sym),
                ("止盈", tp_txt), ("止损", sl_txt),
                ("时间", tstamp),
            ]
        else:
            continue

        # 构建飞书卡片
        # 拿不到值的字段**不渲染** —— 显示「平仓价: —」看起来像失败，实际只是这个
        # action 的 detail 里没有该数据（如 close_all 只给 closed_order_ids）。
        # 注意要连「— USDT」「— 张」这种「空值 + 单位」也一起滤掉。
        def _blank(v) -> bool:
            s = str(v).strip()
            return s == "" or s.startswith("—")

        fields = [(k, v) for k, v in fields if not _blank(v)]
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
    # 进程事件是「此刻发生」的，所以用发送时间；带时区偏移避免 UTC/本地误判
    all_fields = list(fields or []) + [("时间", _fmt_ts(time.time()))]
    field_elements = []
    for i in range(0, len(all_fields), 2):
        pair = all_fields[i:i + 2]
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
