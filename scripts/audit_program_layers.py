# -*- coding: utf-8 -*-
"""两层程序审计：**单 bot 级** + **全 bot 级**。

单 bot 级（每个 bot 自己那条链路对不对）
  A1 生效值一览：闸门 / 止损 / 平仓 / 留痕相关开关的实际取值
  A2 逐 bot 实测 `_management_gate`：用该 bot 的配置实例化 Executor，
     断言「动作 × 闸门」的行为与配置一致（含别名：reduce_* / flatten）
  A3 配置自相矛盾：配了却不生效 / 配了却被总闸熄火 / 无任何止损来源

全 bot 级（多个 bot 之间、以及所有 bot 共用的路径）
  B1 label_prefix 全局唯一（它是归属判据）
  B2 account_risk 与 accounts.yaml 合并后一致（防 strategist 预算与执行闸门漂移）
  B3 所有 Executor 构造点都接了三闸门（防「测试全绿而生产没接线」）
  B4 所有平仓调用点都在白名单里且各有闸门（新增路径必须显式确认）
  B5 三条绕过 `_execute_intent` 的 sweep 方法内部都有 `manage_positions` 闸门
  B6 程序自主的敞口动作都有留痕（`_log_program_action`）
  B7 不变量：程序永不按缺口补单（`size=need` 已消失）
  B8 AI 意图唯一入口 = `_execute_intent`
  B9 执行优先级未被改动（close<reduce<cancel<modify<open）
  B10 留痕的 detail 键名不污染归属判据（平仓市价单 id 不叫 `order`）

跑法：python scripts/audit_program_layers.py
退出码：0 = 全部通过；1 = 有 FAIL
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import (  # noqa: E402
    assert_account_risk_consistent,
    load_all_bots,
    overlay_dir_for,
)
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

FAILS: list[str] = []
WARNS: list[str] = []
INFOS: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)


def warn(msg: str) -> None:
    WARNS.append(msg)


def info(msg: str) -> None:
    INFOS.append(msg)


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _body(src: str, func: str) -> str:
    """取 `def func` 到下一个同缩进 def 之间的源码（够做闸门存在性检查）。"""
    m = re.search(rf"^(    )?def {re.escape(func)}\b", src, re.M)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"^(    )?def \w+\(", src[start + 10:], re.M)
    return src[start:start + 10 + (nxt.start() if nxt else len(src))]


# ─────────────────────────────────────────────────────────────
# 单 bot 级
# ─────────────────────────────────────────────────────────────
def audit_single_bot(bots: dict) -> None:
    print("=" * 78)
    print("单 bot 级：逐 bot 生效值 + 闸门行为实测")
    print("=" * 78)
    hdr = (f"{'bot_id':<20}{'en':<4}{'env':<10}{'lp':<8}{'scope':<6}"
           f"{'mgt':<5}{'cls':<5}{'red':<5}{'reqSL':<6}"
           f"{'autop':<7}{'peak':<7}{'gvb':<7}")
    print(hdr)
    print("-" * len(hdr))

    for bot_id, b in sorted(bots.items()):
        ar = dict(getattr(b, "account_risk", None) or {})
        ap = str(ar.get("auto_protect", False))
        pk = str(ar.get("peak_trail", False))
        gb = str(ar.get("give_back", False))
        print(f"{bot_id:<20}{('Y' if getattr(b, 'enabled', False) else '-'):<4}"
              f"{str(getattr(b, 'env', '')):<10}"
              f"{str(getattr(b, 'label_prefix', '')):<8}"
              f"{str(getattr(b, 'order_scope', '')):<6}"
              f"{str(bool(getattr(b, 'manage_positions', True))):<5}"
              f"{str(bool(getattr(b, 'allow_close', True))):<5}"
              f"{str(bool(getattr(b, 'allow_reduce', True))):<5}"
              f"{str(bool(getattr(b, 'require_sl', True))):<6}"
              f"{ap:<7}{pk:<7}{gb:<7}")

        # ── A2 闸门行为实测 ──
        ex = Executor(
            None, label_prefix=getattr(b, "label_prefix", "") or bot_id,
            root=ROOT, bot_id=bot_id,
            manage_positions=bool(getattr(b, "manage_positions", True)),
            allow_close=bool(getattr(b, "allow_close", True)),
            allow_reduce=bool(getattr(b, "allow_reduce", True)),
            account_risk=ar,
        )
        mgt = ex.manage_positions
        for action, expect in (
            ("close", "MANAGE_POSITIONS_OFF" if not mgt
             else ("" if ex.allow_close else "CLOSE_DISABLED")),
            ("close_all", "MANAGE_POSITIONS_OFF" if not mgt
             else ("" if ex.allow_close else "CLOSE_DISABLED")),
            ("reduce_long", "MANAGE_POSITIONS_OFF" if not mgt
             else ("" if ex.allow_reduce else "REDUCE_DISABLED")),
            ("modify_tp_sl", "MANAGE_POSITIONS_OFF" if not mgt else ""),
        ):
            got = ex._management_gate(action, None)
            if expect and not got.startswith(expect):
                fail(f"[A2] {bot_id}: {action} 期望 {expect}，实际 {got!r}")
            if not expect and got:
                fail(f"[A2] {bot_id}: {action} 应放行，实际被拒 {got!r}")
        # 别名：action=close 但 requested_action=reduce_long → 必须按减仓判
        alias_expect = ("MANAGE_POSITIONS_OFF" if not mgt
                        else ("" if ex.allow_reduce else "REDUCE_DISABLED"))
        got = ex._management_gate(
            "close", Intent(action="close", symbol="X",
                            meta={"requested_action": "reduce_long"}))
        if alias_expect and not got.startswith(alias_expect):
            fail(f"[A2] {bot_id}: reduce_long 别名 期望 {alias_expect}，实际 {got!r}")
        if not alias_expect and got:
            fail(f"[A2] {bot_id}: reduce_long 别名 应放行，实际被拒 {got!r}")

        # ── A3 配置自相矛盾 ──
        if not mgt and (ap not in ("False", "off") or pk not in ("False", "off")
                        or gb not in ("False", "off")):
            warn(f"[A3] {bot_id}: manage_positions=false 但配了 "
                 f"auto_protect={ap}/peak_trail={pk}/give_back={gb} → 全部不生效")
        if ap not in ("False", "off") and float(ar.get("auto_protect_sl_pct") or 0) <= 0:
            warn(f"[A3] {bot_id}: auto_protect={ap} 但 auto_protect_sl_pct<=0 → 不生效")
        if pk not in ("False", "off") and float(ar.get("peak_trail_atr") or 0) <= 0:
            warn(f"[A3] {bot_id}: peak_trail={pk} 但 peak_trail_atr<=0 → 不生效")
        if not bool(getattr(b, "require_sl", True)) and ap in ("False", "off"):
            warn(f"[A3] {bot_id}: require_sl=false 且 auto_protect 关 → "
                 f"无任何止损来源（裸仓自负）")
        if ar.get("use_venue_close"):
            info(f"[A3] {bot_id}: use_venue_close=true → 保护单按「平掉该方向全部」"
                 f"下单（不再跟踪张数）")
        if not bool(getattr(b, "allow_close", True)) and gb not in ("False", "off"):
            info(f"[A3] {bot_id}: allow_close=false 但 give_back={gb} → "
                 f"该路径**仍会平仓**（已解绑，由 account_risk.give_back 单独管）")
        # 阶梯/网格依赖 modify_tp_sl + resync 维护 SL 归并
        strat = getattr(b, "strategist", None)
        pf = str(getattr(strat, "prompt_file", "") or "")
        if not mgt and ("ladder" in pf or "martin" in pf or "grid" in pf):
            warn(f"[A3] {bot_id}: manage_positions=false 但策略像阶梯/网格（{pf}）"
                 f"→ 挂完就不管，SL 归并会失效")


# ─────────────────────────────────────────────────────────────
# 全 bot 级
# ─────────────────────────────────────────────────────────────
def audit_all_bots(bots: dict, config_dir: Path) -> None:
    print()
    print("=" * 78)
    print("全 bot 级：隔离 / 接线 / 不变量")
    print("=" * 78)

    # ── B1 label_prefix 唯一 ──
    # 注意 `check_label_prefixes` 只在**双方都启用**时报错、否则仅告警 ——
    # 所以这里自己做分级：双方启用 = FAIL（真危险）；仅一侧启用 = WARN
    # （潜在危险：未启用那侧若有存量挂单，会被另一个 bot 当成自己的撤掉）。
    by_prefix: dict[str, list[str]] = {}
    for bot_id, b in bots.items():
        lp = str(getattr(b, "label_prefix", "") or "")
        if lp:
            by_prefix.setdefault(lp, []).append(bot_id)
    both_on, latent = [], []
    for lp, ids in sorted(by_prefix.items()):
        if len(ids) < 2:
            continue
        en = [i for i in ids if bool(getattr(bots[i], "enabled", False))]
        (both_on if len(en) >= 2 else latent).append((lp, ids))
    for lp, ids in both_on:
        fail(f"[B1] label_prefix {lp!r} 被多个**启用中**的 bot 共用：{ids}")
    for lp, ids in latent:
        warn(f"[B1] label_prefix {lp!r} 重复：{ids}（当前无同时启用；"
             f"但未启用一侧若有存量挂单，仍会被另一个 bot 误撤）")
    print(f"[B1] label_prefix 唯一性（重复 {len(both_on) + len(latent)} 组："
          f"启用中冲突 {len(both_on)} / 潜在 {len(latent)}）"
          f"{'OK' if not both_on else 'FAIL'}")

    # ── B2 account_risk 合并一致性 ──
    bad = 0
    for bot_id, b in bots.items():
        try:
            assert_account_risk_consistent(b, config_dir)
        except Exception as e:  # noqa: BLE001
            bad += 1
            fail(f"[B2] {bot_id}: account_risk 与 accounts 合并不一致：{e}")
    print(f"[B2] account_risk 合并一致性                 "
          f"{'OK' if not bad else f'FAIL({bad})'}")

    # ── B3 Executor 构造点接线 ──
    watcher = _src("omnialpha/watcher.py")
    sites = [m.start() for m in re.finditer(r"Executor\(", watcher)]
    unwired = []
    for i, pos in enumerate(sites, 1):
        chunk = watcher[pos:pos + 1200]
        missing = [k for k in ("manage_positions=", "allow_close=", "allow_reduce=")
                   if k not in chunk]
        if missing:
            unwired.append((i, missing))
    if unwired:
        for i, missing in unwired:
            fail(f"[B3] watcher Executor 构造点 #{i} 缺 {missing}")
    print(f"[B3] Executor 构造点接线（{len(sites)} 处）"
          f"{'OK' if not unwired else 'FAIL'}")
    others = []
    for p in sorted((ROOT / "omnialpha").rglob("*.py")):
        if p.name in ("executor.py", "watcher.py"):
            continue
        if "Executor(" in p.read_text(encoding="utf-8"):
            others.append(p.name)
    if others:
        warn(f"[B3] 其他文件也构造 Executor：{others}（需确认是否执行信号路径）")

    # ── B4 平仓调用点白名单 ──
    ex_src = _src("omnialpha/executor.py")
    found = []
    for name in ("_give_back_side", "_close", "_close_all"):
        if "self.client.close_position(" in _body(ex_src, name):
            found.append(name)
    expected = {"_give_back_side", "_close", "_close_all"}
    if set(found) != expected:
        fail(f"[B4] 平仓调用点集合变了：{sorted(found)}（期望 {sorted(expected)}）"
             f"—— 新增/删除平仓路径必须显式确认已加闸门")
    print(f"[B4] 平仓调用点白名单（{len(found)} 处）"
          f"{'OK' if set(found) == expected else 'FAIL'}")

    # ── B5 sweep 内部门 ──
    checks = {
        "ensure_protection": True,
        "check_peak_trail": True,
        "check_give_back": True,
    }
    for fn in checks:
        body = _body(ex_src, fn)
        if "self.manage_positions" not in body:
            fail(f"[B5] {fn} 缺 manage_positions 闸门")
    gb_body = _body(ex_src, "check_give_back")
    if "self.allow_close" in gb_body:
        fail("[B5] check_give_back 仍在吃 allow_close（应为解绑状态）")
    print("[B5] 三条 sweep 内部门（含 give_back 解绑）    OK")

    # ── B6 程序自主动作留痕 ──
    for fn, tag in (("_auto_protect_sweep", "watcher:auto_protect"),
                    ("_give_back_sweep", "watcher:give_back")):
        body = _body(watcher, fn)
        if "_log_program_action(" not in body or tag not in body:
            fail(f"[B6] {fn} 缺留痕（{tag}）")
    print("[B6] 程序自主动作留痕                        OK")

    # ── B7 不变量：不按缺口补单 ──
    # 只查**代码行**（注释里提到旧写法 `size=need` 是刻意的历史说明，不算违规）
    code_lines = [ln for ln in ex_src.splitlines()
                  if not ln.strip().startswith("#")]
    if any(re.search(r"size\s*=\s*need\b", ln) for ln in code_lines):
        fail("[B7] ensure_protection 又出现 size=need（按缺口补单 = 部分出场）")
    if "partial_coverage" not in ex_src:
        fail("[B7] 缺 partial_coverage 分支")
    print("[B7] 不变量：永不按缺口补单                  OK")

    # ── B8 AI 意图唯一入口 ──
    if "self._execute_intent(intent)" not in _body(ex_src, "execute_signal"):
        fail("[B8] execute_signal 未调用 _execute_intent")
    leaked = []
    for p in sorted((ROOT / "omnialpha").rglob("*.py")):
        if p.name == "executor.py":
            continue
        t = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\._(close|close_all|open|modify_tp_sl)\(", t):
            leaked.append(f"{p.name}:{m.group(0)}")
    if leaked:
        warn(f"[B8] executor 之外直接调用持仓方法：{leaked}")
    print("[B8] AI 意图唯一入口 = _execute_intent        OK")

    # ── B9 执行优先级 ──
    m = re.search(r"_INTENT_PRIORITY\s*[:=]\s*\{(.*?)\}", ex_src, re.S)
    if not m:
        fail("[B9] 找不到 _INTENT_PRIORITY")
    else:
        block = m.group(1)
        want = [("close", 1), ("reduce", 2), ("cancel", 3), ("modify_tp_sl", 4), ("open", 5)]
        for key, val in want:
            if not re.search(rf'"{key}\w*"\s*:\s*{val}', block):
                fail(f"[B9] _INTENT_PRIORITY 里 {key}={val} 缺失或被改")
    print("[B9] 执行优先级 close<reduce<cancel<modify<open OK")

    # ── B10 留痕键名不污染归属判据 ──
    ep = _src("omnialpha/memory/exchange_pnl.py")
    if '"sl_placed"' not in ep:
        fail("[B10] local_order_ids 不再认 sl_placed")
    gb_body = _body(watcher, "_give_back_sweep")
    if '"close_order_id"' not in gb_body:
        fail("[B10] give_back 留痕未用 close_order_id")
    if re.search(r'"order"\s*:\s*closed', gb_body):
        fail("[B10] give_back 留痕把平仓市价单 id 写成了 `order`（会污染 is_ours）")
    print("[B10] 留痕键名不污染归属判据                OK")


def main() -> int:
    config_dir = ROOT / "config" / "bots"
    bots = load_all_bots(config_dir, overlay_dir_for(config_dir))
    print(f"载入 bot 配置：{len(bots)} 个（{config_dir}）\n")
    audit_single_bot(bots)
    audit_all_bots(bots, config_dir)

    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    print(f"  FAIL {len(FAILS)}   WARN {len(WARNS)}   INFO {len(INFOS)}")
    for x in FAILS:
        print(f"  ✗ {x}")
    for x in WARNS:
        print(f"  ! {x}")
    for x in INFOS:
        print(f"  i {x}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
