# -*- coding: utf-8 -*-
"""10-13 账户归属事故 replay：开/关持仓管理闸门的效果对比。

复刻 BROOKS 账户 2026-10-13 07:18 后的真实状态：
  用户 app 手动开多 ETH 25 张 @2532（无 SL，只有用户自己挂的 TP 提醒单）
bot 按人格 §54「有仓必须给仓位管理结论」给出管理动作：
  ① reduce_long 15（TP1 减半）  ② close 剩余 10  ③ watcher sweep 补 SL

对比三组配置：
  A = 当前线上行为（manage_positions/allow_close/allow_reduce 全默认 true）
  B = manage_positions: false（只开仓、不管仓）
  C = allow_close: false（只禁平仓，减仓/改保护放行）

跑法：python scripts/replay_incident_1013.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402
from omnialpha.strategist.snapshot import collect_snapshot  # noqa: E402


class IncidentClient:
    """复刻事故账户：用户手动多 25 张 @2532，无任何保护单。"""

    env = "paper"

    def __init__(self):
        self.calls: list = []
        self.placed: list[dict] = []
        self.price_orders: list[dict] = []
        self.positions = [{"contract": "ETH_USDT", "size": 25,
                           "entry_price": "2532.0", "mode": "single",
                           "leverage": 20}]
        self._seq = 7000

    def banner(self):
        return "[ETH paper]"

    # ── account ──
    def get_positions(self):
        self.calls.append("get_positions")
        return list(self.positions)

    def get_account(self):
        self.calls.append("get_account")
        return {"available": "500", "total": "500", "position_mode": "single"}

    # ── market ──
    def get_ticker(self, symbol=None):
        return {"mark_price": 2532.0, "last": 2532.0}

    def get_last_price(self, symbol=None):
        return 2532.0

    def get_contract(self, symbol=None):
        return ContractMeta("ETH_USDT", 0.01, 1, 0.01, 50)

    def get_contract_stats(self, symbol=None, limit=1):
        return []

    def get_orderbook_top(self, symbol=None, limit=5):
        return {"bids": [], "asks": []}

    # ── orders ──
    def is_dual_position_mode(self):
        return False

    def get_position_mode(self):
        return "single"

    def close_position(self, contract, side=None, size=0):
        self.calls.append(("close_position", contract, side, size))
        return {"id": "close-x", "fill_price": "2532.0", "pnl": "0"}

    def list_orders(self, contract=None):
        return []

    def list_price_orders(self, contract=None):
        self.calls.append(("list_price_orders", contract))
        return list(self.price_orders)

    def cancel_price_order(self, pid):
        self.calls.append(("cancel_price_order", pid))
        return True

    def place_price_order(self, body):
        self._seq += 1
        o = {"id": self._seq, "status": "open",
             "initial": dict(body.get("initial") or {}),
             "trigger": dict(body.get("trigger") or {})}
        self.price_orders.append(o)
        self.placed.append(body)
        self.calls.append(("place_price_order", self._seq))
        return dict(o)


def run_round(name: str, **gate_kw):
    client = IncidentClient()
    with tempfile.TemporaryDirectory() as td:
        ex = Executor(
            client, label_prefix="bre", root=Path(td), bot_id="brooks-eth",
            account_risk={"auto_protect": True, "auto_protect_sl_pct": 2.0},
            **gate_kw,
        )
        # 模型按人格 §54 对「有仓」给出的管理结论（事故当晚的真实动作序列）
        s1 = ex._execute_intent(Intent(
            action="close", symbol="ETH_USDT", side="long", close_size=15,
            meta={"requested_action": "reduce_long"}))
        s2 = ex._execute_intent(Intent(
            action="close", symbol="ETH_USDT"))
        sweep = ex.ensure_protection("ETH_USDT")
        snap = collect_snapshot(client, ["ETH_USDT"], candles=3,
                                interval="5m",
                                manage_positions=gate_kw.get("manage_positions", True))
    acct = snap["account"]

    print(f"\n{'=' * 64}\n{name}\n{'=' * 64}")
    print(f"  [reduce_long 15]  ok={s1.ok}  detail={s1.detail or s1.error}")
    print(f"  [close 剩余10]    ok={s2.ok}  detail={s2.detail or s2.error}")
    print(f"  [sweep 补SL]      {sweep}")
    print(f"  [模型可见持仓]    {len(acct['positions'])} 条"
          + (f"（{acct.get('position_management')}: "
             f"{acct.get('position_management_note', '')[:40]}…）"
             if acct.get("position_management") else ""))
    print(f"  [交易所实际调用]  {client.calls}")
    print(f"  [真挂出去的单]    {client.placed}")
    return client.calls


class PartialCoverageClient(IncidentClient):
    """部分覆盖异常：多 25 张，但账户里只有一条覆盖 4 张的 SL。

    这是 `ensure_protection` 唯一会产生「程序减仓」的输入 —— 旧逻辑按
    `need = size - covered = 21` 挂一张 21 张的 SL，触发时只平掉 21 张
    = **部分出场**（既非全平、也非结构减仓）。
    """

    def __init__(self):
        super().__init__()
        self.price_orders = [{
            "id": 4242, "status": "open",
            "initial": {"contract": "ETH_USDT", "size": -4, "text": "t-bre-sl",
                        "is_reduce_only": True, "is_close": True},
            "trigger": {"price": "2481.36", "rule": 2, "price_type": 0},
        }]


def run_partial_round():
    """部分覆盖：旧逻辑 vs 新不变量（程序挂出的 SL 张数恒 = 持仓全量）。"""
    import json

    from omnialpha.config import BotConfig
    from omnialpha.watcher import ProjectPaths, _auto_protect_sweep

    client = PartialCoverageClient()
    with tempfile.TemporaryDirectory() as td:
        ex = Executor(
            client, label_prefix="bre", root=Path(td), bot_id="brooks-eth",
            account_risk={"auto_protect": True, "auto_protect_sl_pct": 2.0},
        )
        out = ex.ensure_protection("ETH_USDT")
        legacy_size = 25 - 4          # 旧逻辑的差额补单张数（等价推演，不再执行）

        # watcher 侧：必须让人看见（静默的半裸仓才是真危险）
        client2 = PartialCoverageClient()
        bot = BotConfig(
            bot_id="brooks-eth", env="paper", symbols=["ETH_USDT"],
            label_prefix="bre",
            account_risk={"auto_protect": True, "auto_protect_sl_pct": 2.0},
        )
        bot.create_client = lambda: client2
        paths = ProjectPaths(root=Path(td))
        _auto_protect_sweep(bot, paths, {})
        ap = Path(td) / "data" / "bots" / "brooks-eth" / "state" / "alerts.json"
        alerts = (json.loads(ap.read_text(encoding="utf-8")).get("alerts")
                  if ap.is_file() else [])

    print(f"\n{'=' * 64}\nD｜部分覆盖（持仓 25 张，owned SL 只覆盖 4 张）\n{'=' * 64}")
    print(f"  持仓/覆盖/缺口     25 / {out.get('covered_size')} / {out.get('shortfall')}")
    print(f"  新行为             skipped={out.get('skipped')}  "
          f"真挂出去的单={client.placed}  交易所写操作="
          f"{[c for c in client.calls if 'place_price_order' in str(c)]}")
    print(f"  旧行为（等价推演） 会挂 size={legacy_size} 的差额 SL → 触发即"
          f"「部分出场」（减仓 {legacy_size} 张，剩 4 张）")
    print(f"  watcher 告警       {len(alerts)} 条"
          + (f" → {alerts[0]['type']}: {alerts[0]['detail'][:60]}…" if alerts else ""))
    print(f"  watcher 挂单       {client2.placed}（应为空）")


class GiveBackClient(IncidentClient):
    """多 25 张 @2532，owned SL 2520（1R = |2532−2520|×25×0.01 = 3.0 USD），mark 可变。"""

    def __init__(self, mark: float = 2560.0):
        super().__init__()
        self.mark = mark
        self.price_orders = [{
            "id": 5150, "status": "open",
            "initial": {"contract": "ETH_USDT", "size": -25, "text": "t-bre-sl",
                        "is_reduce_only": True, "is_close": True},
            "trigger": {"price": "2520.0", "rule": 2, "price_type": 0},
        }]

    def get_ticker(self, symbol=None):
        return {"mark_price": self.mark, "last": self.mark}

    def close_position(self, contract, side=None, size=0):
        # Gate 的下单回报通常不带 pnl → 引擎退回按 mark 推算的值（标注 engine）
        self.calls.append(("close_position", contract, side, size))
        return {"id": "close-x", "status": "finished"}


def run_give_back_log_round():
    """程序全平（give_back）→ `trades.jsonl` 必须留下可归因的一行。"""
    import json

    from omnialpha.config import BotConfig
    from omnialpha.watcher import ProjectPaths, _give_back_sweep

    client = GiveBackClient()
    with tempfile.TemporaryDirectory() as td:
        ex = Executor(client, label_prefix="bre", root=Path(td), bot_id="brooks-eth",
                      account_risk={"give_back": True})
        ex.remember_give_back("ETH_USDT", "long", 30)
        bot = BotConfig(bot_id="brooks-eth", env="paper", symbols=["ETH_USDT"],
                        label_prefix="bre", account_risk={"give_back": True})
        bot.create_client = lambda: client
        paths = ProjectPaths(root=Path(td))
        _give_back_sweep(bot, paths, {})       # 武装：mark 2560 → 峰值浮盈 7.0（≥1R）
        client.mark = 2540.0                   # 回撤到 2.0（峰值 7.0 的 28.6%）→ 触发
        _give_back_sweep(bot, paths, {})
        p = Path(td) / "data" / "bots" / "brooks-eth" / "logs" / "trades.jsonl"
        rows = ([json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
                 if x.strip()] if p.is_file() else [])

    print(f"\n{'=' * 64}\nE｜程序全平留痕（give_back → trades.jsonl）\n{'=' * 64}")
    print(f"  平仓调用           {[c for c in client.calls if 'close_position' in str(c)]}")
    print(f"  trades.jsonl 行数  {len(rows)}（此前恒为 0 —— 两条归因链都查不到）")
    if rows:
        st = rows[0]["steps"][0]
        print(f"  source             {rows[0]['source']}")
        print(f"  action / trigger   {st['action']} / {st['detail'].get('trigger')}")
        print(f"  realized_pnl       {st['detail'].get('realized_pnl')}"
              f"（来源 {st['detail'].get('pnl_source')}）")
        print(f"  平仓单 id          {st['detail'].get('close_order_id')}")


def main():
    print("复刻 2026-10-13 BROOKS 账户：用户手动 ETH 多 25 张 @2532，无 SL")
    print("模型动作（人格 §54 强制）：reduce_long 15 → close 剩余 → sweep 补 SL")

    a = run_round("A｜当前线上行为（三闸门全默认 true = 未部署前的老逻辑）")
    b = run_round("B｜manage_positions: false（只开仓、不管仓）",
                  manage_positions=False)
    c = run_round("C｜allow_close: false（只禁平仓；减仓/改保护放行）",
                  allow_close=False)

    print(f"\n{'=' * 64}\n汇总\n{'=' * 64}")
    print(f"  A 交易所写操作调用数: {len([x for x in a if 'close_position' in str(x) or 'place_price_order' in str(x)])}")
    print(f"  B 交易所写操作调用数: {len([x for x in b if 'close_position' in str(x) or 'place_price_order' in str(x)])}  ← 用户手动仓零干预")
    print(f"  C 交易所写操作调用数: {len([x for x in c if 'close_position' in str(x) or 'place_price_order' in str(x)])}  ← 减仓放行、全平被拒")

    run_partial_round()
    run_give_back_log_round()


if __name__ == "__main__":
    main()
