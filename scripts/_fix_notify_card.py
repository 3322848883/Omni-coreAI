# -*- coding: utf-8 -*-
"""修复 notify.py 卡片取值与格式。"""
from pathlib import Path

p = Path(r"C:\Users\w6485\Desktop\测试\gate-signal-bot\gate_bot\monitoring\notify.py")
c = p.read_text(encoding="utf-8")

# 1) close 分支：px 用 _entry_price，pnl 用 _fmt_num
c = c.replace(
    '''        elif action in _CLOSE_ACTIONS:
            px = detail.get("price") or detail.get("avg_price") or ""
            pnl = detail.get("realized_pnl") or detail.get("pnl") or detail.get("pnl_usd") or ""''',
    '''        elif action in _CLOSE_ACTIONS:
            px = _entry_price(detail, s)
            pnl = detail.get("realized_pnl") or detail.get("pnl") or detail.get("pnl_usd") or ""''',
)
c = c.replace(
    '''                ("平仓价", str(px)), ("盈亏", f"{pnl} USDT"),''',
    '''                ("平仓价", px), ("盈亏", f"{_fmt_num(pnl)} USDT"),''',
)

# 2) reduce 分支
c = c.replace(
    '''        elif action in _REDUCE_ACTIONS:
            px = detail.get("price") or ""
            sz = detail.get("size") or detail.get("size_usd") or ""''',
    '''        elif action in _REDUCE_ACTIONS:
            px = _entry_price(detail, s)
            sz = detail.get("size") or detail.get("size_usd") or ""''',
)
c = c.replace(
    '''                ("减仓价", str(px)), ("减仓量", f"{sz} USDT"),''',
    '''                ("减仓价", px), ("减仓量", f"{_fmt_num(sz)} USDT"),''',
)

# 3) modify 分支
c = c.replace(
    '''                ("止盈", str(tp)), ("止损", str(sl)),
            ]
        else:
            continue''',
    '''                ("止盈", _fmt_num(tp)), ("止损", _fmt_num(sl)),
            ]
        else:
            continue''',
)

p.write_text(c, encoding="utf-8")
print("notify.py 卡片取值已修复")
