"""K 线图自动生成：从 kline 数据绘制蜡烛图快照（供 LLM 视觉识别）。

用 Pillow 绘制，无需 matplotlib。输出 PNG 文件供 LLM vision 模型识别。
"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Any, Optional


def generate_candlestick_chart(
    klines: list[dict],
    *,
    symbol: str = "",
    timeframe: str = "",
    width: int = 800,
    height: int = 500,
    output_path: Optional[Path] = None,
) -> bytes:
    """从 K 线数据生成蜡烛图 PNG。

    klines: [{o, h, l, c, v} 或 {open, high, low, close, volume}]
    返回 PNG bytes。如指定 output_path 则同时写文件。
    """
    from PIL import Image, ImageDraw, ImageFont

    # 解析 OHLCV
    candles = []
    for k in klines:
        o = float(k.get("o") or k.get("open") or 0)
        h = float(k.get("h") or k.get("high") or 0)
        l = float(k.get("l") or k.get("low") or 0)
        c = float(k.get("c") or k.get("close") or 0)
        v = float(k.get("v") or k.get("volume") or 0)
        if h > 0 and l > 0:
            candles.append((o, h, l, c, v))

    if not candles:
        return b""

    # 布局常量
    pad_left, pad_right, pad_top, pad_bot = 60, 20, 40, 60
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bot
    vol_h = chart_h // 4
    price_h = chart_h - vol_h - 10  # 10px 间距

    # 价格范围
    all_h = max(c[1] for c in candles)
    all_l = min(c[2] for c in candles)
    price_range = all_h - all_l or 1.0
    price_pad = price_range * 0.05
    y_max = all_h + price_pad
    y_min = all_l - price_pad
    y_range = y_max - y_min or 1.0

    # 成交量范围
    vol_max = max(c[4] for c in candles) or 1.0

    # 画布
    img = Image.new("RGB", (width, height), (18, 18, 24))
    draw = ImageDraw.Draw(img)

    # 字体
    try:
        font = ImageFont.truetype("arial.ttf", 12)
        font_title = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
        font_title = font

    # 标题
    title = f"{symbol} {timeframe}" if symbol or timeframe else ""
    if title:
        draw.text((pad_left, 10), title, fill=(220, 220, 220), font=font_title)

    # 网格线 + 价格刻度
    n_grid = 5
    for i in range(n_grid + 1):
        y = pad_top + int(price_h * i / n_grid)
        price = y_max - (y_range * i / n_grid)
        draw.line([(pad_left, y), (width - pad_right, y)],
                  fill=(40, 40, 50), width=1)
        draw.text((5, y - 8), f"{price:.0f}", fill=(160, 160, 170), font=font)

    # 蜡烛图
    n = len(candles)
    candle_w = max(2, min(12, chart_w // n - 1))
    gap = max(1, (chart_w - n * candle_w) // max(n - 1, 1))

    for i, (o, h, l, c, v) in enumerate(candles):
        x = pad_left + i * (candle_w + gap) + candle_w // 2
        # 颜色
        if c >= o:
            color = (34, 197, 94)    # green
        else:
            color = (239, 68, 68)    # red

        # 影线
        y_h = pad_top + int((y_max - h) / y_range * price_h)
        y_l = pad_top + int((y_max - l) / y_range * price_h)
        draw.line([(x, y_h), (x, y_l)], fill=color, width=1)

        # 实体
        y_o = pad_top + int((y_max - o) / y_range * price_h)
        y_c = pad_top + int((y_max - c) / y_range * price_h)
        y_top, y_bot = min(y_o, y_c), max(y_o, y_c)
        if y_bot - y_top < 1:
            y_bot = y_top + 1
        draw.rectangle([(x - candle_w // 2, y_top), (x + candle_w // 2, y_bot)],
                       fill=color)

        # 成交量
        vol_y_bot = pad_top + price_h + 10 + vol_h
        vol_bar_h = int(v / vol_max * vol_h)
        if vol_bar_h > 0:
            draw.rectangle([(x - candle_w // 2, vol_y_bot - vol_bar_h),
                            (x + candle_w // 2, vol_y_bot)],
                           fill=(*color[:3], 100) if False else color)

    # 成交量分隔线
    y_sep = pad_top + price_h + 5
    draw.line([(pad_left, y_sep), (width - pad_right, y_sep)],
              fill=(60, 60, 70), width=1)
    draw.text((5, y_sep + 2), "Vol", fill=(140, 140, 150), font=font)

    # 保存
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
