"""K 线图自动生成：TradingView 质量蜡烛图（供 LLM 视觉识别）。

白底、细蜡烛、右侧价格轴、底部时间轴、网格线、当前价标记。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Optional


def _load_font(size: int):
    """加载字体，优先 Tahoma/Arial，回退默认。"""
    from PIL import ImageFont
    for name in ("Tahoma.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def generate_candlestick_chart(
    klines: list[dict],
    *,
    symbol: str = "",
    timeframe: str = "",
    width: int = 1200,
    height: int = 700,
    output_path: Optional[Path] = None,
    indicators: Optional[dict] = None,
) -> bytes:
    """从 K 线数据生成 TradingView 风格蜡烛图 PNG。

    indicators: {"ema20": [float, ...], "ema50": [...], ...} 叠加曲线。
    """
    from PIL import Image, ImageDraw

    # ── 解析 OHLCV ──────────────────────────────────
    candles = []
    for k in klines:
        o = float(k.get("o") or k.get("open") or 0)
        h = float(k.get("h") or k.get("high") or 0)
        l = float(k.get("l") or k.get("low") or 0)
        c = float(k.get("c") or k.get("close") or 0)
        v = float(k.get("v") or k.get("volume") or 0)
        t = k.get("t") or k.get("time") or 0
        if h > 0 and l > 0:
            candles.append({"o": o, "h": h, "l": l, "c": c, "v": v, "t": t})

    if not candles:
        return b""

    # ── 布局（TradingView 风格）─────────────────────
    pad_right = 80    # 价格轴
    pad_left = 20
    pad_top = 50      # 标题
    pad_bot = 50      # 时间轴
    vol_ratio = 0.18  # 成交量占比

    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bot
    vol_h = int(chart_h * vol_ratio)
    price_h = chart_h - vol_h - 12  # 12px 间距

    # ── 价格范围 ────────────────────────────────────
    all_h = max(c["h"] for c in candles)
    all_l = min(c["l"] for c in candles)
    price_range = all_h - all_l or 1.0
    price_pad = price_range * 0.08
    y_max = all_h + price_pad
    y_min = all_l - price_pad
    y_range = y_max - y_min or 1.0

    vol_max = max(c["v"] for c in candles) or 1.0

    # ── 高分辨率画布（2x SSAA 抗锯齿）───────────────
    ss = 2
    img = Image.new("RGB", (width * ss, height * ss), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    def S(v):
        return int(v * ss)

    def line(pts, fill, w=1):
        draw.line([(S(p[0]), S(p[1])) for p in pts], fill=fill, width=S(w))

    def rect(box, fill):
        draw.rectangle([(S(box[0]), S(box[1])), (S(box[2]), S(box[3]))], fill=fill)

    def text(pos, txt, fill, font, anchor=None):
        draw.text((S(pos[0]), S(pos[1])), txt, fill=fill, font=font, anchor=anchor)

    # 字体（加大价格标签可读性）
    font_axis = _load_font(16)
    font_title = _load_font(20)
    font_label = _load_font(17)
    font_indicator = _load_font(13)

    # ── 标题 ────────────────────────────────────────
    title = f"{symbol} · {timeframe}" if symbol or timeframe else ""
    if title:
        text((pad_left, 12), title, (30, 30, 30), font_title)

    # ── 网格线（浅灰虚线）───────────────────────────
    n_grid = 6
    grid_color = (220, 220, 220)
    for i in range(n_grid + 1):
        y = pad_top + int(price_h * i / n_grid)
        # 虚线效果：短线段
        for x0 in range(pad_left, width - pad_right, 8):
            line([(x0, y), (min(x0 + 4, width - pad_right), y)], grid_color)

    # ── 价格轴（右侧，TradingView 风格）─────────────
    price_decimals = 1 if price_range < 100 else 0
    for i in range(n_grid + 1):
        y = pad_top + int(price_h * i / n_grid)
        price = y_max - (y_range * i / n_grid)
        text((width - pad_right + 8, y), f"{price:,.{price_decimals}f}",
             (80, 80, 80), font_axis, anchor="lm")

    # 价格轴竖线
    line([(width - pad_right, pad_top), (width - pad_right, pad_top + price_h)],
         (180, 180, 180))

    # ── 蜡烛图 ──────────────────────────────────────
    n = len(candles)
    body_w = max(2, min(16, chart_w // n - 2))
    gap = max(1, (chart_w - n * body_w) // max(n - 1, 1))
    wick_w = 1

    GREEN = (38, 166, 91)     # TV 风格绿
    RED = (214, 60, 60)       # TV 风格红

    for i, cd in enumerate(candles):
        x = pad_left + i * (body_w + gap) + body_w // 2
        color = GREEN if cd["c"] >= cd["o"] else RED

        # 影线（细线）
        y_h = pad_top + int((y_max - cd["h"]) / y_range * price_h)
        y_l = pad_top + int((y_max - cd["l"]) / y_range * price_h)
        line([(x, y_h), (x, y_l)], color, wick_w)

        # 实体（精确边界）
        y_o = pad_top + (y_max - cd["o"]) / y_range * price_h
        y_c = pad_top + (y_max - cd["c"]) / y_range * price_h
        y_top, y_bot = min(y_o, y_c), max(y_o, y_c)
        if y_bot - y_top < 1.5:
            y_bot = y_top + 1.5
        rect((x - body_w / 2, y_top, x + body_w / 2, y_bot), color)

    # ── 指标叠加线（EMA 等）─────────────────────────
    if indicators:
        # 指标颜色配置（TradingView 风格，高对比度）
        ind_colors = {
            "ema20": (255, 120, 0),     # 鲜橙
            "ema50": (30, 100, 240),    # 鲜蓝
            "ema200": (156, 39, 176),   # 紫色
            "sma20": (255, 120, 0),
            "sma50": (30, 100, 240),
            "sma200": (156, 39, 176),
        }
        for name, values in indicators.items():
            if not values or len(values) < 2:
                continue
            color = ind_colors.get(name.lower(), (100, 100, 100))
            pts = []
            for i, v in enumerate(values):
                if i >= n or v is None:
                    continue
                x = pad_left + i * (body_w + gap) + body_w // 2
                y = pad_top + (y_max - float(v)) / y_range * price_h
                pts.append((x, y))
            if len(pts) >= 2:
                # 细白底衬（1px）保证线在蜡烛上可见
                for j in range(len(pts) - 1):
                    line([pts[j], pts[j + 1]], (255, 255, 255), 3)
                # 主线（粗 3px，TV 风格）
                for j in range(len(pts) - 1):
                    line([pts[j], pts[j + 1]], color, 3)
                # 标签：线尾右侧，带白色圆角背景
                label = name.upper()
                lx, ly = pts[-1][0] + 6, pts[-1][1]
                # 测量文字宽度做背景
                bbox = draw.textbbox((0, 0), label, font=font_indicator)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                # 白底
                rect((lx - 2, ly - th // 2 - 3, lx + tw + 4, ly + th // 2 + 3),
                     (255, 255, 255))
                text((lx, ly), label, color, font_indicator, anchor="lm")

    # ── 成交量（半透明风格）─────────────────────────
    vol_top = pad_top + price_h + 12
    vol_bot = vol_top + vol_h

    # 成交量分隔线
    line([(pad_left, vol_top), (width - pad_right, vol_top)], (200, 200, 200))
    text((pad_left, vol_top - 16), "Vol", (150, 150, 150), font_axis)

    GREEN_LIGHT = (200, 230, 210)
    RED_LIGHT = (245, 210, 210)

    for i, cd in enumerate(candles):
        x = pad_left + i * (body_w + gap) + body_w // 2
        vol_bar_h = cd["v"] / vol_max * vol_h
        color = GREEN_LIGHT if cd["c"] >= cd["o"] else RED_LIGHT
        rect((x - body_w / 2, vol_bot - vol_bar_h, x + body_w / 2, vol_bot), color)

    # ── 时间轴（底部）───────────────────────────────
    line([(pad_left, height - pad_bot), (width - pad_right, height - pad_bot)],
         (180, 180, 180))

    n_labels = min(8, n)
    for i in range(n_labels):
        idx = int(i * (n - 1) / max(n_labels - 1, 1))
        x = pad_left + idx * (body_w + gap) + body_w // 2
        t = candles[idx].get("t", 0)
        if t > 1e12:
            t = t / 1000  # ms → s
        if t > 0:
            import datetime
            dt = datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc)
            label = dt.strftime("%m-%d %H:%M") if timeframe in ("1m", "5m", "15m", "1h") else dt.strftime("%m-%d")
        else:
            label = f"{idx}"
        text((x, height - pad_bot + 10), label, (100, 100, 100), font_axis, anchor="ma")

    # ── 当前价标记（红色虚线 + 标签）────────────────
    last_price = candles[-1]["c"]
    y_price = pad_top + (y_max - last_price) / y_range * price_h
    # 虚线
    for x0 in range(pad_left, width - pad_right, 6):
        line([(x0, y_price), (min(x0 + 3, width - pad_right), y_price)], (220, 50, 50))
    # 标签
    rect((width - pad_right + 2, y_price - 12, width - 2, y_price + 12), (220, 50, 50))
    text((width - pad_right + 6, y_price), f"{last_price:,.{price_decimals}f}",
         (255, 255, 255), font_label, anchor="lm")

    # ── 降采样回目标分辨率（抗锯齿）─────────────────
    img = img.resize((width, height), Image.LANCZOS)

    # ── 保存 ────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(png_bytes)

    return png_bytes


def chart_to_base64(png_bytes: bytes) -> str:
    """PNG bytes → base64 data URI（供 LLM vision API）。"""
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def generate_and_encode(
    klines: list[dict], *, symbol: str = "", timeframe: str = "",
    output_path: Optional[Path] = None,
) -> Optional[str]:
    """一步生成图并返回 base64 data URI（失败返回 None）。"""
    try:
        png = generate_candlestick_chart(
            klines, symbol=symbol, timeframe=timeframe, output_path=output_path,
        )
        if not png:
            return None
        return chart_to_base64(png)
    except Exception:  # noqa: BLE001
        return None
