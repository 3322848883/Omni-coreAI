# -*- coding: utf-8 -*-
"""尺寸四口径入口的**实网**核实（只读公开接口，不下单）。

用法：
    NO_PROXY=69.12.85.185,localhost,127.0.0.1 PYTHONPATH=. \
        python scripts/_verify_size_resolve.py

验的是「交易所真实合约规格 → 解析出的张数」这条链，不依赖 fixture：
`get_contracts` 会带 `X-Gate-Size-Decimal: 1` 拉真实 `order_size_min`，
再把它喂给 `usd_to_contracts` / `Executor._resolve_order_size`（纯计算，无签名请求）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.executor import Executor                     # noqa: E402
from omnialpha.gate_client import ContractMeta, GateApiError, GateClient  # noqa: E402
from omnialpha.schema import Intent                          # noqa: E402
from omnialpha.sizing import quantize_size, safe_step, usd_to_contracts  # noqa: E402

SYMBOLS = ["ETH_USDT", "BTC_USDT", "SOL_USDT", "XRP_USDT", "DOGE_USDT", "PEPE_USDT"]
ok_n = fail_n = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"  [PASS] {name} {detail}")
    else:
        fail_n += 1
        print(f"  [FAIL] {name} {detail}")


class _NoNetwork:
    """只为 `_resolve_order_size` 的「取最新价」兜底分支准备；正常不触发。"""

    env = "live"

    def __init__(self, prices):
        self.prices = prices

    def get_last_price(self, symbol):
        return self.prices[symbol]

    def get_available_usdt(self):
        return 100.0

    def get_contract(self, symbol):
        raise AssertionError("不该再取合约")

    def get_account(self):
        return {"total": "100", "balance": "100"}


def main() -> int:
    cli = GateClient(api_key="", api_secret="", env="live")
    metas = cli.get_contracts(max_age_sec=0) or {}
    print(f"合约元数据：{len(metas)} 个（带 X-Gate-Size-Decimal: 1）\n")

    print("① 规格字段（交易所真实值）")
    for s in SYMBOLS:
        m = metas.get(s)
        if not m:
            check(f"{s} 在列表里", False)
            continue
        print(f"  {s:<11} 乘数={m.quanto_multiplier:<10g} 步长={safe_step(m):<5g} "
              f"min={m.size_min:<6g} max={m.size_max:<12g} decimal={m.enable_decimal}")
        check(f"{s} 步长>0", safe_step(m) > 0)
        check(f"{s} step ≤ min 或 min 是 step 整数倍",
              m.size_min <= 0 or abs(m.size_min / safe_step(m) - round(m.size_min / safe_step(m))) < 1e-9,
              f"min={m.size_min} step={safe_step(m)}")

    # 用真实价格算名义，避免拿假价格得到「看起来对」的结论
    prices = {}
    for s in SYMBOLS:
        try:
            prices[s] = float(cli.get_last_price(s))
        except Exception as e:  # noqa: BLE001
            print(f"  [SKIP] {s} 取价失败：{e}")
            prices[s] = 0.0

    print("\n② usd_to_contracts：永远落在步长上、且不小于最小可下量")
    for s in SYMBOLS:
        m, px = metas.get(s), prices.get(s)
        if not m or not px:
            continue
        for usd in (17.0, 3.0, 0.01):
            q = usd_to_contracts(usd, px, m)
            step = safe_step(m)
            on_step = abs(q / step - round(q / step)) < 1e-9
            print(f"  {s:<11} size_usd={usd:<6g} → {q:g} 张 "
                  f"({q * px * m.quanto_multiplier:.2f}U)")
            check(f"{s} ${usd:g} 落在步长上", on_step, f"q={q} step={step}")
            check(f"{s} ${usd:g} ≥ 最小可下量", q >= float(m.size_min or 0) - 1e-9)

    print("\n③ _resolve_order_size：四口径 + 冲突/非法出声")
    gate = _NoNetwork(prices)
    root = Path(tempfile.mkdtemp(prefix="sz_"))
    ex = Executor(gate, root=root, bot_id="verify")

    for s in SYMBOLS:
        m, px = metas.get(s), prices.get(s)
        if not m or not px:
            continue
        step = safe_step(m)
        q, notional, basis = ex._resolve_order_size(
            Intent(action="open_long", symbol=s, size=step * 7), m)
        check(f"{s} size={step * 7:g} → {q:g}", abs(q - step * 7) < 1e-9, f"basis={basis}")

        q2, _n, b2 = ex._resolve_order_size(
            Intent(action="open_long", symbol=s, size_usd=17.0), m, entry_hint=px)
        check(f"{s} size_usd=17 → {q2:g} 张", q2 > 0 and abs(q2 / step - round(q2 / step)) < 1e-9,
              f"basis={b2}")

        try:
            ex._resolve_order_size(Intent(action="open_long", symbol=s,
                                          size=step, size_usd=17.0), m, entry_hint=px)
            check(f"{s} 双口径冲突被拒", False)
        except GateApiError as e:
            check(f"{s} 双口径冲突被拒", "SIZE_CONFLICT" in str(e))

        try:
            ex._resolve_order_size(Intent(action="open_long", symbol=s, size=step / 100), m)
            check(f"{s} 小于步长被拒", False)
        except GateApiError as e:
            check(f"{s} 小于步长被拒", "SIZE_TOO_SMALL" in str(e))

        try:
            ex._resolve_order_size(Intent(action="open_long", symbol=s), m)
            check(f"{s} 无口径被拒", False)
        except GateApiError as e:
            check(f"{s} 无口径被拒", "SIZE_REQUIRED" in str(e))

    print("\n④ quantize_size 一致性（floor / round / 抬到 min）")
    for s in ("ETH_USDT", "BTC_USDT"):
        m = metas[s]
        step = safe_step(m)
        for raw in (step * 7.4, step * 7.6, step * 0.4):
            f = quantize_size(raw, m, mode="floor", at_least_step=False)
            r = quantize_size(raw, m, mode="round", at_least_step=False)
            check(f"{s} floor({raw:g})={f:g} ≤ raw", f <= raw + 1e-9)
            check(f"{s} round({raw:g})={r:g} 在半步内", abs(r - raw) <= step / 2 + 1e-9)

    print(f"\n===== PASS {ok_n} / FAIL {fail_n} =====")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
