# -*- coding: utf-8 -*-
"""核实：网格止盈的两套「限价平仓」形态是否都成立。
   A 条件限价（price_order + order_type=limit）  B 挂单限价（place_order reduce_only）
只读/本地：用桩客户端捕获订单体，不正连交易所。
"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omnialpha.executor import Executor, Intent, infer_trigger_rules  # noqa: E402
from omnialpha.gate_client import ContractMeta  # noqa: E402


class C:
    def __init__(self, mark):
        self.bodies, self.mark = [], mark
    def list_price_orders(self, c=None): return []
    def get_order(self, oid): return {"id": oid, "finish_as": "filled"}
    def get_ticker(self, s): return {"mark_price": self.mark, "last": self.mark}
    def get_contract(self, s):
        return ContractMeta(name="ETH_USDT", quanto_multiplier=0.01,
                            order_size_round=1, order_price_round=0.1, leverage_max=100)
    def place_price_order(self, body):
        self.bodies.append(("A", body))
        ini = body["initial"]
        return {"id": "tpA", "price": ini["price"], "size": ini["size"],
                "finish_as": "untriggered", "text": ini["text"]}
    def place_order(self, body):
        self.bodies.append(("B", body)); return {"id": "tpB", "finish_as": "open"}
    def get_orderbook_top(self, s, limit=1):
        return {"bids": [{"p": "2371"}], "asks": [{"p": "2373"}]}


def main():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data/bots/mg/state").mkdir(parents=True)
    # 模拟「2355 档已成交」：mark 落到 2355，AI 早已写下目标 2367
    (tmp / "data/bots/mg/state/ladder_levels.json").write_text(json.dumps({"levels": [
        {"symbol": "ETH_USDT", "price": 2355.0, "size": 1, "tp_target": 2367.0,
         "label": "mtg", "order_id": "o1", "ts": "2026-10-15T00:00:00Z", "status": "pending"}]}),
        encoding="utf-8")
    cli = C(mark=2355.0)
    ex = Executor(cli, label_prefix="mtg", root=tmp, bot_id="mg",
                  account="", account_risk={"grid_tp_auto": "live"})
    out = ex.ensure_grid_tp("ETH_USDT")

    print("### A 条件限价（ensure_grid_tp 实际下单）")
    for tag, b in cli.bodies:
        if tag == "A":
            print(json.dumps(b, ensure_ascii=False, indent=1))

    # B：挂单限价（直接调 _place_limit_exit）
    meta = cli.get_contract("ETH_USDT")
    it = Intent(action="open_long", symbol="ETH_USDT", side="long", label="mtg")
    rec = ex._place_limit_exit(it, "long", 2367.0, is_tp=True, meta=meta, size=1)
    print("\n### B 挂单限价（_place_limit_exit）")
    print(json.dumps(rec.get("body"), ensure_ascii=False, indent=1))

    print("\n### 逐档合法性推演（现价 mark=2372，长仓 TP 必须 > mark）")
    ladder = [(2355, 2367.0), (2331.5, 2349.0), (2300, 2307.25), (2290, 2295.0),
              (2262, 2271.0), (2248, 2255.0), (2235, 2241.5)]
    print(f"{'档位':>8} {'AI目标':>9} {'成交前(mark 2372)':>18} {'成交后(mark≈档位)':>18}")
    for lvl, tgt in ladder:
        before = "非法(目标<mark)" if tgt < 2372 else "合法"
        after = "非法" if tgt <= lvl else "**合法 ✔**"
        print(f"{lvl:>8} {tgt:>9} {before:>18} {after:>19}")
    print("\nDONE")


if __name__ == "__main__":
    main()
