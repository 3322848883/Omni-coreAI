# -*- coding: utf-8 -*-
"""本地端到端实跑：**网格空窗修复**（martin-grid 形态，走真实入口）。

与 `tests/test_replace_no_empty_grid.py`（单元/离线集成）互补：这个脚本从
**inbox 信号**出发，跑 `run_bot_once → process_file → execute_signal → 真 paper 引擎`，
逐轮打「网格数/保护单/持仓/权益」并检查不变量 —— 是修复的**本地验收**。

剧本（4 轮，全部带 `replace: "symbol"` —— 正是旧 bridge 与配置型会产生的最难形态）：

  R1 铺网格        10 张 open_long(limit) + 1 张市价成交建仓        → 期望网格 10（重铺）
  R2 管理轮        仅 modify_tp_sl                                 → 期望网格**仍 10**（旧代码这里归 0）
  R3 铺单轮对照    open_long × 10                                  → 期望旧梯被替换（同分→不同 id）
  R4 显式撤单      cancel_all                                      → 期望网格**清零**（显式意图照撤）

另附**契约层核对**：`chips_to_signal` 对「只改保护」的 plan 必须产出 `replace=none`。

跑法：python scripts/e2e_local_grid_window.py
退出码：0 = 全部 PASS；1 = 有 FAIL。
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SYM = "BTC_USDT"
BOT = "gw-paper"
OK, BAD = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (OK if cond else BAD).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return cond


# martin-grid 形态：place_before_cancel 开、default_replace none、own 作用域
BASELINE = """\
bot_id: {bot}
enabled: false
env: paper
symbols: [{sym}]
label_prefix: gwl
position_policy: free
default_replace: none
place_before_cancel: true
order_scope: own
require_sl: true
account_risk:
  no_flip: false
  max_leverage: 50
paper:
  feed_exchange: gate
  initial_capital: 10000
  leverage: 20
"""

OVERLAY = "enabled: true\n"


def grid_orders(n: int, mark: float) -> list[dict]:
    """10 档限价网格（价格由**当前 mark** 推出，落在 paper 校验带 ±5% 内）。

    paper 引擎按 `标价×quanto` 校验上下 5%（BTC quanto=0.0001 → 张值≈8.3U）。
    """
    out = []
    for i in range(n):
        px = round(mark * (1 - 0.005 - i * 0.003), 1)   # -0.5% … -3.2%
        out.append({
            "action": "open_long", "symbol": SYM, "type": "limit", "price": px,
            "size_usd": 9, "sl": round(mark * 0.97, 1), "tp": round(mark * 1.03, 1),
            "leverage": 20, "tp_mode": "trigger", "sl_mode": "trigger",
            "tp_type": "market", "sl_type": "market",
            "trigger_price_type": "mark", "label": "g",
        })
    return out


def _grid_ids(client, prefix: str) -> list[str]:
    """本 bot 的**普通挂单**（网格）id。注意 `list_orders` 只返回未终结的普通单，
    条件单（TP/SL）走 `list_price_orders`，不会混进来。text 形如 `t-<prefix>[-<label>]`。"""
    pfx = f"t-{prefix}"
    return sorted(str(o.get("id")) for o in (client.list_orders(SYM) or [])
                  if str(o.get("text") or "").startswith(pfx))


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="    [%(levelname)s] %(message)s")
    from omnialpha.config import load_all_bots
    from omnialpha.watcher import ProjectPaths, run_bot_once

    root = Path(tempfile.mkdtemp(prefix="omnialpha_gw_"))
    try:
        (root / "config" / "bots").mkdir(parents=True)
        (root / "config" / "bots.local").mkdir(parents=True)
        inbox = root / "data" / "bots" / BOT / "inbox"
        inbox.mkdir(parents=True)
        (root / "config" / "bots" / f"{BOT}.yaml").write_text(
            BASELINE.format(bot=BOT, sym=SYM), encoding="utf-8")
        (root / "config" / "bots.local" / f"{BOT}.yaml").write_text(OVERLAY, encoding="utf-8")

        paths = ProjectPaths(root)
        bot = load_all_bots(paths.config_dir)[BOT]
        client = bot.create_client()

        def mark() -> float:
            return float(client.get_last_price(SYM))

        base = mark()

        def put(i: int, payload: dict) -> None:
            (inbox / f"{i:02d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            run_bot_once(bot, paths)

        def rows_log(tag: str) -> tuple[int, int, int]:
            n = len(_grid_ids(client, bot.label_prefix))
            prot = len(client.list_price_orders(SYM) or [])
            pos = sum(abs(int(p["size"])) for p in (client.get_positions() or [])
                      if (p.get("contract") == SYM))
            print(f"  {tag:<26} 网格={n:<3} 保护单={prot:<3} 持仓={pos}")
            return n, prot, pos

        print("=" * 92)
        print(f"本地端到端实跑：网格空窗修复（{BOT} / {SYM} / mark={base}）")
        print("=" * 92)

        # ── R1 铺网格（10 张限价）+ 市价建仓 1 张 ──
        put(1, {"replace": "symbol", "orders": grid_orders(10, mark()), "meta": {}})
        # 直接补一张已成交仓位（模拟某档已成交），管理轮才有对象
        client.place_order({"contract": SYM, "size": 1, "type": "market", "text": "t-gwl"})
        n1, _p1, pos1 = rows_log("R1 铺网格")
        grid1 = _grid_ids(client, bot.label_prefix)
        check("R1 铺出 10 张网格", n1 == 10, f"n={n1}")
        check("R1 有持仓（管理轮的对象）", pos1 >= 1, f"pos={pos1}")

        # ── R2 管理轮：只有 modify_tp_sl（replace 仍写 symbol —— 最难形态）──
        put(2, {"replace": "symbol", "orders": [{
            "action": "modify_tp_sl", "symbol": SYM, "side": "long",
            "tp": round(mark() * 1.06, 1), "sl": round(mark() * 0.97, 1),
            "label": "m"}], "meta": {}})
        n2, _p2, _pos2 = rows_log("R2 管理轮(仅 modify)")
        grid2 = _grid_ids(client, bot.label_prefix)
        check("★ 管理轮后网格**原样保留**（10 张、id 不变）",
              grid2 == grid1, f"{len(grid2)} 张；新增/丢失={set(grid2) ^ set(grid1)}")
        check("管理轮 TP 已更新（保护单仍在）", len(client.list_price_orders(SYM) or []) >= 1)

        # ── R3 铺单轮对照：open_long × 10 → 旧梯应被替换 ──
        put(3, {"replace": "symbol", "orders": grid_orders(10, mark()), "meta": {}})
        n3, _p3, _pos3 = rows_log("R3 铺单轮(对照)")
        grid3 = _grid_ids(client, bot.label_prefix)
        check("R3 铺单轮仍是 10 张（新梯接替）", n3 == 10, f"n={n3}")
        check("R3 确认发生了替换（id 与上一轮不同）", grid3 != grid2,
              f"重叠={len(set(grid3) & set(grid2))}")

        # ── R4 显式撤单：cancel_all → 应照撤（显式意图不受守卫影响）──
        put(4, {"replace": "symbol", "orders": [
            {"action": "cancel_all", "symbol": SYM, "label": "c"}], "meta": {}})
        n4, _p4, _pos4 = rows_log("R4 显式撤单")
        check("R4 显式 cancel_all 照撤（网格清零）", n4 == 0, f"n={n4}")

        # ── 契约层核对：bridge 对「只改保护」的 plan 必须产出 replace=none ──
        from omnialpha.strategist.bridge import chips_to_signal
        from omnialpha.strategist.risk import RiskResult
        from omnialpha.strategist.schema import Chip, Plan
        mgmt = Chip(symbol=SYM, action="modify_tp_sl", tp=mark() * 1.05)
        entry = Chip(symbol=SYM, action="open_long", size_usd=9, price=mark() * 0.98)
        r_mgmt = chips_to_signal(Plan(cycle_id="c1", reasoning="r", chips=[mgmt]),
                                 RiskResult(accepted=[mgmt]), bot_id=BOT)["replace"]
        r_entry = chips_to_signal(Plan(cycle_id="c2", reasoning="r", chips=[entry]),
                                  RiskResult(accepted=[entry]), bot_id=BOT)["replace"]
        print(f"  bridge 契约：管理轮 replace={r_mgmt!r} / 铺单轮 replace={r_entry!r}")
        check("bridge 管理轮 → replace=none", r_mgmt == "none", r_mgmt)
        check("bridge 铺单轮 → replace=symbol", r_entry == "symbol", r_entry)

        # ── 留痕与隔离 ──
        tj = root / "data" / "bots" / BOT / "logs" / "trades.jsonl"
        steps = []
        for line in (tj.read_text(encoding="utf-8").splitlines() if tj.is_file() else []):
            if line.strip():
                steps += json.loads(line).get("steps") or []
        skips = [s for s in steps if (s.get("detail") or {}).get("skipped") == "no_replacement"]
        check("执行层守卫有留痕（skipped=no_replacement）", bool(skips),
              str(skips[0].get("detail", {})) if skips else "无")
        rc = [s for s in steps if s.get("action") == "replace_cancel"
              and (s.get("detail") or {}).get("cancelled")]
        check("R3/R4 的撤旧动作仍正常发生（未被守卫误杀）",
              len(rc) >= 1, f"{len(rc)} 次")
        check("paper 账户库落在临时根内",
              (root / "data" / "bots" / BOT / "paper" / "account.db").is_file())
        check("未污染仓库 data/", not (ROOT / "data" / "bots" / BOT).exists())

        print()
        print("=" * 92)
        print(f"结论：PASS {len(OK)}  FAIL {len(BAD)}")
        for b in BAD:
            print(f"  ✗ {b}")
        print("=" * 92)
        return 1 if BAD else 0
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
