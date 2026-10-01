# -*- coding: utf-8 -*-
"""通知卡片字段完整性验证 —— 所有类型、所有字段不能缺失。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate_bot.monitoring import format_trade_card, format_process_card  # noqa: E402


def extract_fields(card: dict) -> dict:
    """把卡片压平成 {字段名: 值}，方便检查空值。"""
    out = {}
    for el in card.get("elements") or []:
        for f in (el.get("fields") or []):
            content = f.get("text", {}).get("content", "")
            if "**" in content and ":**" in content:
                key = content.split("**")[1].replace(":", "").strip()
                val = content.split("**")[-1].strip()
                out[key] = val
    return out


def check(label: str, card: dict, required: list) -> bool:
    fields = extract_fields(card)
    print(f"\n--- {label} ---")
    for k, v in fields.items():
        empty = (v in ("", "—", "None", "0", "0.0") and k not in ("方向",))
        mark = "❌ 空" if (v == "" or v == "None") else ("⚠️ " if v == "—" else "✅")
        print(f"  {mark} {k}: {v}")
    missing = [k for k in required if k not in fields or fields[k] in ("", "None")]
    if missing:
        print(f"  ❌ 缺失: {missing}")
        return False
    print(f"  ✅ 必填字段齐全 {required}")
    return True


def main() -> None:
    ok = True

    # 1) 开仓多
    cards = format_trade_card("brooks-btc", [{
        "action": "open_long", "ok": True, "symbol": "BTC_USDT",
        "detail": {"entry_price": 83130, "sl": 82800, "tp": 83950,
                   "size_usd": 325.82624228780105, "order": {"fill_price": 83130}},
    }])
    ok &= check("开仓·多", cards[0], ["Bot", "币种", "方向", "入场价", "仓位", "止损", "止盈"])

    # 2) 开仓空（只有 entry_price）
    cards = format_trade_card("brooks-btc", [{
        "action": "open_short", "ok": True, "symbol": "BTC_USDT",
        "detail": {"entry_price": 83350.0, "sl": 83720.0, "tp": 82560.0,
                   "size_usd": 24373.295400837662},
    }])
    ok &= check("开仓·空", cards[0], ["Bot", "币种", "方向", "入场价", "仓位", "止损", "止盈"])

    # 3) 止盈平仓
    cards = format_trade_card("brooks-btc", [{
        "action": "close", "ok": True, "symbol": "BTC_USDT",
        "detail": {"realized_pnl": 2.87, "price": 82560.0},
    }])
    ok &= check("止盈平仓", cards[0], ["Bot", "币种", "平仓价", "盈亏"])

    # 4) 止损平仓
    cards = format_trade_card("brooks-btc", [{
        "action": "close", "ok": True, "symbol": "BTC_USDT",
        "detail": {"realized_pnl": -1.5, "order": {"fill_price": 83720}},
    }])
    ok &= check("止损平仓", cards[0], ["Bot", "币种", "平仓价", "盈亏"])

    # 5) 减仓
    cards = format_trade_card("brooks-btc", [{
        "action": "reduce_long", "ok": True, "symbol": "ETH_USDT",
        "detail": {"price": 3200, "size": 5},
    }])
    ok &= check("减仓", cards[0], ["Bot", "币种", "减仓价", "减仓量"])

    # 6) 改单
    cards = format_trade_card("brooks-btc", [{
        "action": "modify_tp_sl", "ok": True, "symbol": "BTC_USDT",
        "detail": {"tp": 82560, "sl": 83720},
    }])
    ok &= check("改单", cards[0], ["Bot", "币种", "止盈", "止损"])

    # 7) 进程卡片
    card = format_process_card("restart", "自动拉起 brooks-btc/plan",
                               [("Bot", "brooks-btc"), ("组件", "plan"), ("动作", "已自动重启")], "green")
    ok &= check("进程·重启", card, ["Bot", "组件", "动作"])

    card = format_process_card("storm", "brooks-btc/plan 停手",
                               [("Bot", "brooks-btc"), ("组件", "plan"),
                                ("重启次数", "5"), ("处置", "请人工介入")], "red")
    ok &= check("进程·风暴", card, ["Bot", "组件", "重启次数", "处置"])

    print("\n" + "=" * 50)
    print("全部字段完整 ✅" if ok else "有字段缺失 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
