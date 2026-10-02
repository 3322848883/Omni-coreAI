from pathlib import Path
p = Path("omnialpha/strategist/tv_indicators/__init__.py")
t = p.read_text(encoding="utf-8")
t = t.replace(
    "from .lr_ha_candles import (\n    alma,\n    heikin_ashi,\n    hma,\n    kama,\n    lr_candles,\n    lr_ha_candles,\n    t3_moving_average,\n    volatility_bands,\n)",
    "from .lr_ha_candles import (\n    heikin_ashi,\n    lr_candles,\n    lr_ha_candles,\n    t3_moving_average,\n    volatility_bands,\n)\nfrom ..indicators import alma, hma, kama",
)
t = t.replace(
    '"heikin_ashi", "lr_candles", "lr_ha_candles",\n    "t3_moving_average", "volatility_bands",\n    "kama", "hma", "alma",',
    '"heikin_ashi", "lr_candles", "lr_ha_candles",\n    "t3_moving_average", "volatility_bands",\n    "kama", "hma", "alma",',
)
p.write_text(t, encoding="utf-8")
print("done")