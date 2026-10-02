"""对照 TV 三张截图输出核对数值。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.tv_indicators import (
    heikin_ashi,
    linreg_channel,
    lr_ha_candles,
    rsi_base,
    rsi_bollinger,
    rsi_macd,
    rsi_ma,
    swing_structure,
    t3_moving_average,
    volatility_bands,
)

d = json.loads((ROOT / "verify_data" / "tv_inputs.json").read_text(encoding="utf-8"))
o, h, l, c = d["bars"]["open"], d["bars"]["high"], d["bars"]["low"], d["bars"]["close"]
t = d["bars"]["time_utc"]

rsi = rsi_base(c, 14)
ma = rsi_ma(rsi, 21, "SMA")
bb = rsi_bollinger(rsi, 21, 2.0)
sw = swing_structure(rsi, 3, 3)
mac = rsi_macd(rsi, 12, 26, 9)

print("=== 图1 RSI Yata (14/close/3/21 SMA/2, MACD 12-26-9) 最后6根 ===")
for i in range(-6, 0):
    lab = sw[i] or "-"
    def f(v):
        return "-" if v is None else f"{v:.4f}"
    print(f"{t[i]}  close={c[i]:.1f}  rsi={f(rsi[i])}  ma21={f(ma[i])}  "
          f"bb_up={f(bb['upper'][i])}  bb_dn={f(bb['lower'][i])}  "
          f"macd_hist={f(mac['hist'][i])}  struct={lab}")

ch = linreg_channel(c, h, l, length=20)
print()
print("=== 图3 Linreg Channel 3-layer (20, 1/2/3 sigma) 末值 ===")
print(f"base    start={ch['base']['start']:.4f}  end={ch['base']['end']:.4f}")
for lay in ch["layers"]:
    print(f"{lay['mult']:.0f}sigma  upper_end={lay['upper']['end']:.4f}  "
          f"lower_end={lay['lower']['end']:.4f}")
print(f"slope={ch['slope']:.6f}  std={ch['std_dev']:.4f}  pearson_r={ch['pearson_r']:.6f}")

ha = heikin_ashi(o, h, l, c)
lrha = lr_ha_candles(o, h, l, c, length=9)
t3 = t3_moving_average(c, 5, 0.7)
vb = volatility_bands(h, l, c, 20)
print()
print("=== 图2 LR HA Candles (9) + T3(5,0.7) + VB(20) 末值 ===")
print(f"ha_close={ha['close'][-1]:.4f}  ha_open={ha['open'][-1]:.4f}")
print(f"lrha_close={lrha['close'][-1]:.4f}  lrha_open={lrha['open'][-1]:.4f}")
print(f"t3={t3[-1]:.4f}  vb_basis={vb['basis'][-1]:.4f}  "
      f"vb_upper={vb['upper_inner'][-1]:.4f}")

# 结构标签全文
print()
print("=== RSI swing_structure 全序列（非空） ===")
for i, lab in enumerate(sw):
    if lab:
        print(f"  bar{i:2d} {t[i]}  rsi={rsi[i]:.4f}  -> {lab}")
