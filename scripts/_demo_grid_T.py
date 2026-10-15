# -*- coding: utf-8 -*-
"""演示：网格做 T 的效果（旧占位 TP vs 新逐档条件限价 TP）。

跑**真实代码路径**：`_skip_placeholder_tp` / `_open` 的占位跳过 / `ensure_grid_tp` /
`_place_trigger` 的订单体。桩客户端只捕获订单，不连交易所。
"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omnialpha.executor import Executor, _skip_placeholder_tp  # noqa: E402
from omnialpha.gate_client import ContractMeta  # noqa: E402

Q = 0.01          # ETH_USDT quanto_multiplier
FEE = 0.00071     # 实测单边费率


def fee(x):
    return x * FEE


class C:
    def __init__(self, mark):
        self.mark, self.bodies = mark, []
    def list_price_orders(self, c=None): return []
    def get_order(self, oid): return {"id": oid, "finish_as": "filled"}
    def get_ticker(self, s): return {"mark_price": self.mark, "last": self.mark}
    def get_contract(self, s):
        return ContractMeta(name="ETH_USDT", quanto_multiplier=Q,
                            order_size_round=1, order_price_round=0.1, leverage_max=100)
    def place_price_order(self, b):
        self.bodies.append(b)
        i = b["initial"]
        return {"id": "tpX", "price": i["price"], "size": i["size"],
                "finish_as": "untriggered", "text": i["text"]}


def body_of(ex, level, target):
    """模拟：level 成交后 ensure_grid_tp 挂出的订单体。"""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data/bots/mg/state").mkdir(parents=True)
    (tmp / "data/bots/mg/state/ladder_levels.json").write_text(json.dumps({"levels": [
        {"symbol": "ETH_USDT", "price": level, "size": 1, "tp_target": target, "label": "mtg",
         "order_id": "o1", "ts": "2026-10-15T00:00:00Z", "status": "pending"}]}), encoding="utf-8")
    ex.root = tmp
    ex._place_exit_leg = lambda placer, kind, price_order: (placer(), None)
    out = ex.ensure_grid_tp("ETH_USDT")
    return (ex.client.bodies[-1] if ex.client.bodies else None), out


def main():
    LADDER = [(2355, 2367.0), (2331.5, 2349.0), (2314.5, None), (2300, 2307.25),
              (2290, 2295.0), (2280, None), (2262, 2271.0), (2248, 2255.0),
              (2235, 2241.5), (2222, None)]
    print("=" * 78)
    print("场景：ETH 现价 2372，阶梯 10 档；AI 已把每格目标写进 meta.tp_target")
    print("（None = 马丁档，共用 TP，不在本演示范围）")
    print("=" * 78)

    # ---------- 旧行为 ----------
    print("\n【A 旧行为】入场腿带「占位 TP」")
    ph = round(2372 * 1.03, 1)
    print(f"  挂 2355 买单时带 TP = 现价×1.03 = {ph}  ← 与真实目标 2367 无关")
    print(f"  价格跌到 2355 成交 → 持仓 1 张 @2355，交易所上 TP 仍在 {ph}")
    print(f"  → 止盈挂在 {(ph/2355-1)*100:+.2f}% 处；做 T 不发生；若 LLM 不改它就是「假网格」")

    # ---------- 新行为 ----------
    print("\n【B 新行为】入场腿不挂占位（grid_tp_auto=live 且是网格腿）")
    for lvl, tgt in LADDER[:2]:
        if tgt is None:
            continue
        skip = _skip_placeholder_tp({"grid_tp_auto": "live"}, {"tp_target": tgt})
        print(f"  _skip_placeholder_tp(live, tp_target={tgt}) = {skip}  → 不挂占位 TP ✔")
    skip_m = _skip_placeholder_tp({"grid_tp_auto": "live"}, {"tp_target": None})
    print(f"  _skip_placeholder_tp(live, 马丁腿=None) = {skip_m}  → 马丁腿照挂共用 TP ✔")

    print("\n  成交后 → ensure_grid_tp 按 AI 的目标挂【条件限价】单：")
    ex = Executor(C(mark=2355.0), label_prefix="mtg", root=Path(tempfile.mkdtemp()),
                  bot_id="mg", account="", account_risk={"grid_tp_auto": "live"})
    body, out = body_of(ex, 2355.0, 2367.0)
    print("    " + json.dumps(body, ensure_ascii=False))
    print(f"    → placed: {json.dumps(out['placed'], ensure_ascii=False)}")

    # ---------- 逐档成品 ----------
    print("\n【C 多个成交档 → 各自一条 TP（触发价各不相同）= 真网格】")
    rows = []
    for lvl, tgt in LADDER:
        if tgt is None:
            continue
        ex2 = Executor(C(mark=float(lvl)), label_prefix="mtg", root=Path(tempfile.mkdtemp()),
                       bot_id="mg", account="", account_risk={"grid_tp_auto": "live"})
        b, o = body_of(ex2, float(lvl), tgt)
        if b:
            gross = (tgt - lvl) * Q
            net = gross - fee(lvl * Q) - fee(tgt * Q)
            rows.append((lvl, tgt, tgt - lvl, gross, net))
    print(f"  {'档位':>8} {'AI目标':>9} {'往返点':>7} {'毛利(U)':>9} {'净利(U)':>9}")
    tot = 0.0
    for lvl, tgt, d, g, n in rows:
        tot += n
        print(f"  {lvl:>8} {tgt:>9} {d:>7.2f} {g:>9.4f} {n:>9.4f}")
    print(f"  {'合计':>8} {'':>9} {'':>7} {'':>9} {tot:>9.4f}   ← 7 个网格档各做一次 T")

    print("\n【D 对照：中值口径（改动 3 之前）】")
    tot_mid = 0.0
    for lvl, tgt in LADDER:
        if tgt is None:
            continue
        m = lvl + (tgt - lvl) / 2
        tot_mid += (tgt - lvl) / 2 * Q - fee(lvl * Q) - fee(m * Q)
    print(f"  7 档合计净利 ≈ {tot_mid:.4f} U  （整格 {tot:.4f} U）→ 整格是它的 {tot/tot_mid:.1f} 倍")
    print("\nDONE")


if __name__ == "__main__":
    main()
