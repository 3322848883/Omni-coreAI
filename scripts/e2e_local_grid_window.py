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

第二阶段（**逐档止盈 / 假网格修复**）在同一 paper 账户上继续：

  S1  三笔市价入场（走 _open）                    → 期望 P1 档位表落盘逐档一条
  A   modify_tp_sl 三档全合法（现价上方）         → 期望 3 条各自止盈、合计=持仓
  B   reduce_long 减 1 张                         → 期望持仓 -1、TP 合计随持仓对齐
  C   hold                                        → 期望零变化（TP/持仓不动）
  D   modify_tp_sl 一档在现价下方（长仓非法）     → 期望跳过该腿、合法腿照挂 + 留痕
  E   马丁共用 TP（单腿、tp1_share=1.0）          → 期望 1 条 TP 覆盖全仓（含网格）
  P2  account 工具注入 ladder_levels              → 期望 LLM 快照可见逐档表

跑法：python scripts/e2e_local_grid_window.py
退出码：0 = 全部 PASS；1 = 有 FAIL。
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _retry(fn, tries: int = 4, delay: float = 1.5):
    """网络抖动（Gate 偶发 SSL EOF）会让市价成交/行情取数失败 → 重试。

    paper 引擎的市价成交要走 `orderbook` 取成交价（真实网络），本脚本因此
    天然依赖出网；把抖动重试掉，避免把环境问题误判成逻辑回归。
    """
    last: Exception | None = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay)
    raise last if last else RuntimeError("retry failed")

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


def _tp_orders(client, prefix: str) -> list[tuple[float, int]]:
    """本 bot 的**逐档止盈单**（trigger, 张数绝对值），按触发价排序。

    条件单走 `list_price_orders`；只认 text 以 `t-<prefix>` 开头、`-tp` 结尾的
    未终结单（`-sl` 保护单不计入）。这是「网格每档自己的止盈」的直接观测点。

    触发价要兼容两种视图：Gate 实盘是嵌套 `initial/trigger`，paper 引擎
    `_price_order_view` 是**扁平**顶层 `trigger_price`。
    """
    pfx = f"t-{prefix}"
    out: list[tuple[float, int]] = []
    for o in (client.list_price_orders(SYM) or []):
        init = o.get("initial") or {}
        text = str(init.get("text") or o.get("text") or "")
        if not text.startswith(pfx) or not text.endswith("-tp"):
            continue
        if str(o.get("status") or "").lower() in (
                "cancelled", "finished", "filled", "triggered", "failed", "closed"):
            continue
        trig = o.get("trigger") or {}
        px = float(trig.get("price") or init.get("trigger_price")
                   or o.get("trigger_price") or 0)
        sz = abs(int(float(init.get("size") or o.get("size") or 0)))
        out.append((px, sz))
    return sorted(out)


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
            return _retry(lambda: float(client.get_last_price(SYM)))

        base = mark()

        def put(i: int, payload: dict) -> None:
            """投一轮。网络抖动（Gate 偶发 SSL EOF）会让 `run_bot_once` 抛出
            （paper 引擎的市价成交/取价都走真实行情）→ 吞掉并留 warn，
            调用方用 `put_until` 的条件重投。"""
            (inbox / f"{i:02d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                run_bot_once(bot, paths)
            except Exception as e:  # noqa: BLE001
                print(f"    [warn] round {i} raised: {str(e)[:90]}")

        def put_until(i: int, payload: dict, cond, tries: int = 3) -> None:
            """网络抖动会让整轮失败 → 重投，直到 cond() 成立或次数用尽。

            重投用递增索引（i、i+1、i+2），调用方要留出间隔避免与后续轮撞号。
            """
            for k in range(tries):
                put(i + k, payload)
                if cond():
                    return

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
        grid_payload = {"replace": "symbol", "orders": grid_orders(10, mark()), "meta": {}}
        put_until(1, grid_payload,
                  lambda: len(_grid_ids(client, bot.label_prefix)) == 10)
        # 直接补一张已成交仓位（模拟某档已成交），管理轮才有对象
        try:
            _retry(lambda: client.place_order(
                {"contract": SYM, "size": 1, "type": "market", "text": "t-gwl"}))
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] seed position raised: {str(e)[:90]}")
        n1, _p1, pos1 = rows_log("R1 铺网格")
        grid1 = _grid_ids(client, bot.label_prefix)
        check("R1 铺出 10 张网格", n1 == 10, f"n={n1}")
        check("R1 有持仓（管理轮的对象）", pos1 >= 1, f"pos={pos1}")

        # ── R2 管理轮：只有 modify_tp_sl（replace 仍写 symbol —— 最难形态）──
        mgmt_payload = {"replace": "symbol", "orders": [{
            "action": "modify_tp_sl", "symbol": SYM, "side": "long",
            "tp": round(mark() * 1.06, 1), "sl": round(mark() * 0.97, 1),
            "label": "m"}], "meta": {}}
        put_until(5, mgmt_payload,
                  lambda: len(client.list_price_orders(SYM) or []) >= 1)
        n2, _p2, _pos2 = rows_log("R2 管理轮(仅 modify)")
        grid2 = _grid_ids(client, bot.label_prefix)
        check("★ 管理轮后网格**原样保留**（10 张、id 不变）",
              grid2 == grid1, f"{len(grid2)} 张；新增/丢失={set(grid2) ^ set(grid1)}")
        check("管理轮 TP 已更新（保护单仍在）", len(client.list_price_orders(SYM) or []) >= 1)

        # ── R3 铺单轮对照：open_long × 10 → 旧梯应被替换 ──
        put_until(8, {"replace": "symbol", "orders": grid_orders(10, mark()), "meta": {}},
                  lambda: len(_grid_ids(client, bot.label_prefix)) == 10
                  and _grid_ids(client, bot.label_prefix) != grid2)
        n3, _p3, _pos3 = rows_log("R3 铺单轮(对照)")
        grid3 = _grid_ids(client, bot.label_prefix)
        check("R3 铺单轮仍是 10 张（新梯接替）", n3 == 10, f"n={n3}")
        check("R3 确认发生了替换（id 与上一轮不同）", grid3 != grid2,
              f"重叠={len(set(grid3) & set(grid2))}")

        # ── R4 显式撤单：cancel_all → 应照撤（显式意图不受守卫影响）──
        put_until(14, {"replace": "symbol", "orders": [
            {"action": "cancel_all", "symbol": SYM, "label": "c"}], "meta": {}},
            lambda: len(_grid_ids(client, bot.label_prefix)) == 0)
        n4, _p4, _pos4 = rows_log("R4 显式撤单")
        check("R4 显式 cancel_all 照撤（网格清零）", n4 == 0, f"n={n4}")

        # ═══════════ 第二阶段：逐档止盈（假网格修复验收）═══════════
        print()
        print("-" * 92)
        print("第二阶段：网格逐档止盈（_align_tp_group / P1 档位表 / P2 快照注入）")
        print("-" * 92)

        def mkt_entry(label: str) -> dict:
            m = mark()
            # size_usd 给足（≥2 张），避免不同价位下张数取整成 0 → 入场被拒
            return {"action": "open_long", "symbol": SYM, "size_usd": 25,
                    "sl": round(m * 0.97, 1), "tp": round(m * 1.06, 1),
                    "leverage": 20, "tp_mode": "trigger", "sl_mode": "trigger",
                    "tp_type": "market", "sl_type": "market",
                    "trigger_price_type": "mark", "label": label}

        def pos_size() -> int:
            return sum(abs(int(float(p.get("size") or 0)))
                       for p in (client.get_positions() or [])
                       if p.get("contract") == SYM)

        # S1 三笔市价入场（走 _open → P1 逐档记录）；网络抖动 → 逐笔重投
        pos_before = pos_size()          # 第一阶段残留 1 张 → 基线要扣掉
        for i, lb in ((11, "L1"), (21, "L2"), (31, "L3")):
            _b = pos_size()
            put_until(i, {"replace": "none", "orders": [mkt_entry(lb)], "meta": {}},
                      lambda b=_b: pos_size() > b)
        pos0 = pos_size()
        check("S1 三笔市价入场建仓（持仓增加）", pos0 >= pos_before + 3,
              f"pos {pos_before}→{pos0}")

        lv_path = root / "data" / "bots" / BOT / "state" / "ladder_levels.json"
        lv = (json.loads(lv_path.read_text(encoding="utf-8")).get("levels") or []
              if lv_path.is_file() else [])
        check("P1 档位表落盘（逐档一条，≥3）", len(lv) >= 3, f"n={len(lv)}")
        check("P1 档位字段完整（symbol/size/order_id）",
              all(x.get("symbol") == SYM and int(x.get("size") or 0) >= 1
                  and str(x.get("order_id") or "") for x in lv[:3]),
              str(lv[:1]))

        # 场景 A：三档止盈全部合法（现价上方）→ 3 条各自挂出
        m = mark()
        put_until(41, {"replace": "none", "orders": [{
            "action": "modify_tp_sl", "symbol": SYM, "side": "long",
            "tp": round(m * 1.02, 1), "tp2": round(m * 1.03, 1), "tp3": round(m * 1.04, 1),
            "tp1_share": 1 / 3, "tp2_share": 1 / 3, "tp3_share": 1 / 3,
            "sl": round(m * 0.97, 1), "label": "A"}], "meta": {}},
            lambda: len(_tp_orders(client, bot.label_prefix)) == 3)
        tps_a = _tp_orders(client, bot.label_prefix)
        check("A 三档止盈全部挂出（3 条不同触发价）",
              len(tps_a) == 3 and len({p for p, _ in tps_a}) == 3, str(tps_a))
        check("A 三档张数合计 = 持仓（不超配）",
              sum(s for _, s in tps_a) == pos0, f"{tps_a} vs pos={pos0}")

        # 场景 B：减仓 1 张 → _close → _resync_protectors → TP 组对齐
        # paper 市价成交要 orderbook（走网络）；抖动会让整轮 failed → 重投
        reduce_payload = {"replace": "none", "orders": [{
            "action": "reduce_long", "symbol": SYM, "size": 1, "label": "B"}], "meta": {}}
        put_until(51, reduce_payload, lambda: pos_size() < pos0)
        pos_b = pos_size()
        tps_b = _tp_orders(client, bot.label_prefix)
        check("B 减仓 1 张（持仓 -1）", pos_b == pos0 - 1, f"pos {pos0}→{pos_b}")
        check("B 减仓后 TP 张数合计 = 新持仓（逐档不归并、不超配）",
              sum(s for _, s in tps_b) == pos_b and len(tps_b) >= 1,
              f"{tps_b} vs pos={pos_b}")

        # 场景 C：hold 轮 → 零变化
        put(61, {"replace": "none", "orders": [
            {"action": "hold", "symbol": SYM, "label": "C"}], "meta": {}})
        check("C hold 轮零变化（TP/持仓不动）",
              _tp_orders(client, bot.label_prefix) == tps_b and pos_size() == pos_b,
              f"tps={_tp_orders(client, bot.label_prefix)} pos={pos_size()}")

        # 场景 D：三档里一档在现价下方（长仓 TP rule=1 非法）→ 跳过该腿、合法腿照挂
        m = mark()
        put_until(71, {"replace": "none", "orders": [{
            "action": "modify_tp_sl", "symbol": SYM, "side": "long",
            "tp": round(m * 1.02, 1), "tp2": round(m * 1.03, 1),
            "tp3": round(m * 0.99, 1),          # 现价下方 → 非法腿
            "tp1_share": 1 / 3, "tp2_share": 1 / 3, "tp3_share": 1 / 3,
            "sl": round(m * 0.97, 1), "label": "D"}], "meta": {}},
            lambda: len(_tp_orders(client, bot.label_prefix)) == 2)
        tps_d = _tp_orders(client, bot.label_prefix)
        check("D 非法腿被跳过、合法腿照挂（2 条且都在现价上方）",
              len(tps_d) == 2 and all(p > m for p, _ in tps_d), str(tps_d))

        # 场景 E：马丁共用 TP = **单腿**、share 1.0 → 覆盖全仓（含网格）
        m = mark()
        put_until(81, {"replace": "none", "orders": [{
            "action": "modify_tp_sl", "symbol": SYM, "side": "long",
            "tp": round(m * 1.05, 1), "tp1_share": 1.0,
            "sl": round(m * 0.97, 1), "label": "M"}], "meta": {}},
            lambda: len(_tp_orders(client, bot.label_prefix)) == 1)
        tps_e = _tp_orders(client, bot.label_prefix)
        check("E 马丁单腿全仓 TP（1 条、张数 = 持仓）",
              len(tps_e) == 1 and tps_e[0][1] == pos_size(),
              f"{tps_e} vs pos={pos_size()}")

        # P2：account 工具按 bot 级路径注入档位表（LLM 逐档对账的输入）
        from omnialpha.strategist.tools import run_tool
        acc = run_tool(client, "account", {"symbols": [SYM]}, env="paper",
                       bot_root=root, bot_id=BOT, symbols=[SYM])
        n_lv = len((acc or {}).get("ladder_levels") or [])
        if not n_lv:
            print(f"    [diag] account keys={sorted((acc or {}).keys())} "
                  f"msg={(acc or {}).get('message')!r} err={(acc or {}).get('error')!r} "
                  f"file_exists={lv_path.is_file()}")
        check("P2 account 快照注入 ladder_levels（bot 级路径）", n_lv >= 1, f"n={n_lv}")

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
        sk_illegal = [s for s in steps
                      if (s.get("detail") or {}).get("skipped_illegal")]
        check("D 非法腿跳过有留痕（skipped_illegal）", bool(sk_illegal),
              str(sk_illegal[0].get("detail", {})) if sk_illegal else "无")
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
