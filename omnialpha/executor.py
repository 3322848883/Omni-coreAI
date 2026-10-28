"""Execute parsed intents against Gate.io."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .gate_client import GateApiError, GateClient, position_qty, resolve_symbol
from .observability import swallow
from .schema import (
    Intent,
    SignalFile,
    expand_signal,
    infer_trigger_rules,
    tp_levels_of,
)
from .sizing import (
    default_trigger_limit_price,
    floor_to_step,
    pct_to_size_usd,
    quantize_size,
    round_price,
    safe_step,
    usd_to_contracts,
    vol_adjust_size,
)

log = logging.getLogger("omnialpha.executor")


def _is_flat_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "no position" in msg or "position_empty" in msg


def _num(v: Any) -> float:
    """把「张数」解析成**带符号** float，取不到返回 0.0。

    **不能用 `int()`**（2026-10-26 实测）：
      - 小数张合约（`enable_decimal`，ETH/SOL/XRP/PEPE… 共 15 个，`order_size_min=0.1`）
        下 Gate 返回的是字符串 `"-0.7"`，`int("-0.7")` 直接抛 ValueError；
      - 即使拿到的是 float，`int(0.7)` 也会截成 0 —— 那会让「有 0.7 张持仓」
        被读成「空仓」，平仓/保护单/对账全部失效。
    """
    if v is None or isinstance(v, bool):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt_qty(x: float) -> str:
    """张数的字符串形态：整数 → `"3"`，小数 → `"0.7"`。

    Gate 的 `amount` / `size` 是字符串字段。`str(3.0)` = `"3.0"` 能过但格式不规范，
    而且日志/对账里与 `"3"` 不相等会引出假差异。
    """
    v = round(float(x or 0), 8)
    return str(int(v)) if v == int(v) else f"{v:g}"


def qty(v: Any) -> float:
    """张数绝对值（小数张安全）。见 `_num` 的说明。"""
    return abs(_num(v))

PRICE_TYPE_MAP = {"latest": 0, "mark": 1, "index": 2}

# 权益比例上限（`account_risk.max_notional_pct`）的**微越界容差**。
# `loop._prompt_risk` 把「权益 × pct」的精确值交给 AI，AI 会顶格用它；而权益在
# 「出价 → 执行」之间会因浮盈/浮亏漂移，上限跟着收紧一点点 —— 顶格那笔就被
# `_check_notional` 整笔拒掉（2026-10-07 实盘 10h 内 8 次，全是模型想开的仓）。
# 只钳这个比例以内的越界（取整 / 权益漂移）；再大就是计划本身有问题，仍交给
# `_check_notional` 拒单，护栏不会被磨平。
_NOTIONAL_DRIFT_TOL = 0.01

# 浮盈回撤保护的观测过期时间：超过这么久没观测过，历史峰值不可信（进程停过 /
# 扫描没跑到），重新起算。与 `_PEAK_TRAIL_STALE_SEC` 同口径。
_GIVE_BACK_STALE_SEC = 900.0

# 锁盈触发单的 text 后缀。单独一个 tail 是**刻意的**：tp/sl/lp/ls 那套归属过滤
# （`_owned_sl_orders_sided` / `_cleanup_orphan_protectors` / `_resync_protectors`）
# 会把它当成保护腿去归并、改张数，而它由本文件自己按峰值 ratchet 管理。
_GIVE_BACK_TAIL = "gb"

# 触发单的终结状态（列表接口已按 status=open 过滤，这里是对 paper 全量台账的兜底）
_DEAD_PO_STATUS = ("cancelled", "finished", "filled", "triggered", "failed", "closed")


@dataclass
class StepResult:
    action: str
    symbol: str
    ok: bool
    detail: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class ExecReport:
    results: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "steps": [
                {
                    "action": r.action,
                    "symbol": r.symbol,
                    "ok": r.ok,
                    "detail": r.detail,
                    "error": r.error,
                }
                for r in self.results
            ],
        }


# 峰值跟踪的连续性上限（秒）：两次观测间隔超过它，历史高点就不可信了 ——
# 中间可能平过仓又开过新仓，而扫描没跑到。宁可重新起算（= 不动作），
# 也不要拿一个来路不明的旧高点去卡当前仓位。默认 3× watcher 的 300s 扫描间隔。
_PEAK_TRAIL_STALE_SEC = 900.0


def _to_float(v: Any) -> float:
    """宽松转 float：取不到返回 0.0（交易所字段可能是字符串、None 或缺失）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _sl_move_rejected(pos_side: str, entry: float, new_sl: float,
                      old_sl: float = 0.0, mark: float = 0.0,
                      policy: str = "strict") -> str:
    """SL 移动纪律（用户 2026-10-12 晚复盘修正版；2026-10-13 加 ladder 分派）。

    用户规则原话：「止损移动应该只允许在盈利保本的时候」——判据是**持仓是否
    浮盈**（mark vs entry），不是新 SL 相对 entry。实盘 18:01 实例：浮盈
    +7.7 点时 SL 2539.3→2545.9（仍 < entry 2551.2）是合法锁盈（锁住 5.3 点），
    首版判据（新 SL vs entry）会把它误拒、逼持仓裸奔到更远的旧 SL。

    规则（多头；空头镜像）：
    1. **浮盈才许动**：mark > entry 才能改 SL；mark 取不到（0）时退回保守
       判据（新 SL >= entry）。
    2. **只许收紧方向**：新 SL 必须 > 旧 SL（放宽/同值一律拒绝——同值重挂
       churn 在这一步顺带被拦）。
    3. **即时触发护栏**：新 SL >= mark 会挂上即触发平仓，拒绝。

    `policy`（2026-10-13，阶梯「没跑满 10 层就止损」修复）：
      - ``"strict"``（默认，brooks 类单腿策略）：上面三条全保留。
      - ``"ladder"``（阶梯/马丁网格）：**跳过浮盈 gate**（网格就是低吸建仓，
        浮亏是常态）并**允许往深挪（widen）**——被保护单路径（peak_trail）
        推浅的 SL 挪回「最深档之外」是**必要修正**，不是风险放宽。仍保留：
        ① 同值 no-op 拒绝（churn）；② 即时触发护栏（挂上即触发仍拒）。
      为什么必须分开：网格的 SL 是「全梯共用的一条最后防线」，语义与单腿
      移动止损不同；用单腿纪律去管它，结果是浮亏时全线锁死、浅 SL 修不掉。

    返回非空 reason = 拒绝；"" = 放行。
    """
    is_long = pos_side == "long"
    ladder = str(policy or "strict").strip().lower() in (
        "ladder", "grid", "widen", "free")
    if not ladder and old_sl > 0:
        # **只有「移动既有 SL」才受浮盈纪律约束**。`old_sl == 0` 表示该方向
        # 此刻**没有**任何 SL（裸仓）—— 这是「从无到有补保护」，不是「亏损时
        # 放宽止损」，拒掉等于让仓位一直裸奔，而且 `_modify_tp_sl` 会把它报成
        # `ok:true` 的良性 no-op（静默丢腿）。
        # 实测 2026-10-27 08:32 pa-eth-v5：0.1 张空腿裸仓，AI 要求挂硬 SL
        # 2324.72，被判 "not floating (mark 2312.94 vs entry 2311.05)" 挡下
        # → 空腿继续无保护。护栏仍由下面两条兜住：即时触发 + （有旧 SL 时）收紧方向。
        if mark > 0:
            floating = (mark > entry) if is_long else (mark < entry)
            if not floating:
                return (f"not floating (mark {mark} vs entry {entry}); "
                        "SL move only allowed in profit")
        else:
            # mark 缺失（老交易所响应/测试桩）→ 退回首版保守判据
            if is_long and new_sl < entry:
                return f"new_sl {new_sl} < entry {entry} (mark unknown; conservative)"
            if not is_long and new_sl > entry:
                return f"new_sl {new_sl} > entry {entry} (mark unknown; conservative)"
    # 即时触发护栏
    if mark > 0:
        if is_long and new_sl >= mark:
            return (f"new_sl {new_sl} >= mark {mark} "
                    "(long SL would trigger instantly)")
        if not is_long and new_sl <= mark:
            return (f"new_sl {new_sl} <= mark {mark} "
                    "(short SL would trigger instantly)")
    # 方向纪律
    if old_sl > 0:
        if ladder:
            # ladder：允许往深挪（widen），只拦同值 no-op（churn）
            if abs(new_sl - old_sl) < 1e-6:
                return f"new_sl {new_sl} == old_sl {old_sl} (no-op churn blocked)"
        else:
            # strict：只许收紧方向（同值 = 无效 churn，一并拒绝）
            if is_long and new_sl <= old_sl:
                return f"new_sl {new_sl} <= old_sl {old_sl} (long widen/no-op blocked)"
            if not is_long and new_sl >= old_sl:
                return f"new_sl {new_sl} >= old_sl {old_sl} (short widen/no-op blocked)"
    return ""


def _apportion_sizes(total: float, weights: list[float],
                     step: float = 1.0) -> list[float]:
    """最大余数法：把 `total` 按 `weights` 比例分成 `len(weights)` 份。

    - 合计**恒等于** total —— 多一分就会挂出合计超过持仓的 reduce-only 单
      （`_tp_legs` 的同款不变量：「绝不挂出合计超过持仓量的 reduce-only 单」）
    - 允许 0 份：调用方把 0 张腿撤掉（与 `_tp_legs` 「拆不出每腿 ≥1 张就退回」
      同构 —— 持仓张数不够分时，保大弃小而不是硬拆）
    - `total <= 0` 或 weights 全 0 → 全 0

    用于 TP 腿组对齐（`_align_tp_group`）：逐档止盈每腿各平自己那份，
    减仓后按原比例缩，而不是把每腿都放大到全仓。

    **`step`（2026-10-26）**：全程按「一个步长的整数倍」分配。原来按整数张算，
    ETH 持仓 0.7 张时 `total=0.7` 会被补成 **1 张** → 挂出合计 1 张的 reduce-only TP，
    而持仓只有 0.7 张 → 交易所直接拒（超平）。步长 1 的合约（BTC/DOGE）行为不变。
    """
    n = len(weights)
    st = float(step or 0) or 1.0
    if n == 0 or total <= 0:
        return [0.0] * n
    units = [max(0, int(round(float(w) / st))) for w in weights]
    tot_u = max(0, int(round(float(total) / st)))
    wsum = sum(units)
    if wsum <= 0 or tot_u <= 0:
        return [0.0] * n
    raw = [tot_u * u / wsum for u in units]
    sizes = [int(x) for x in raw]          # 向下取整：和 ≤ total，差 ≥ 0
    diff = tot_u - sum(sizes)
    if diff > 0:
        # 小数余数大的先补；并列按列表序（确定性，测试可复现）
        order = sorted(range(n), key=lambda i: raw[i] - sizes[i], reverse=True)
        i = 0
        while diff > 0:
            sizes[order[i % n]] += 1
            diff -= 1
            i += 1
    return [round(u * st, 8) for u in sizes]


def _protector_side(init: dict) -> Optional[str]:
    """保护单平的是哪个方向（`long` / `short` / `None`）。

    **`auto_size` 优先**：venue-computed 保护单（`size=0` + `auto_size`）的 `size`
    恒为 0，靠符号推方向必然失败 —— 旧代码 `if raw_sz == 0: continue` 会把这类腿
    整条丢掉，保护审计随即认为「没有 SL」→ 每轮重挂，无限 churn。
    缺 `auto_size` 时才退回符号：负 = 卖 = 平多，正 = 买 = 平空。
    """
    a = str(init.get("auto_size") or "").strip().lower()
    if a == "close_long":
        return "long"
    if a == "close_short":
        return "short"
    sz = _num(init.get("size"))
    if sz < 0:
        return "long"
    if sz > 0:
        return "short"
    return None


def _parse_symbol_positions(positions: list, symbol: str, meta: Any = None) -> list[dict]:
    """从 `/positions` 原始记录里取出该 symbol 的**非零**持仓。

    `Executor._symbol_positions` 与撤单闸门共用这一份解析，避免两条路径漂移。
    输出**总是带 `contract`** —— 这样孤儿判定可以「永远比对 contract」，
    而不必靠「持仓记录里有没有 contract」去反推它来自哪条路径（那种推断
    一旦多出第三方调用方就会静默失效）。

    **张数用 `position_qty` 反算真实值**（`value/(mark×quanto)`）：
    `/positions` 的 `size` 是 int64，小数张合约会被截掉小数部分 —— 实测 ETH
    真实 0.1 张的幽灵多仓 `size` 返回 **0**，真实 16.1 张的空仓返回 **-16**。
    按 `size` 过滤会让前一条**在整个引擎里彻底消失**（保护审计 / 孤儿清理 /
    `_resync_protectors` / `ensure_protection` 全看不见），它于是永远得不到保护、
    也永远不会被清掉。
    """
    out = []
    for p in positions or []:
        if p.get("contract") != symbol:
            continue
        size = position_qty(p, meta)
        if size == 0:
            continue
        mode = str(p.get("mode") or "")
        if size > 0:
            side = "long"
        elif size < 0:
            side = "short"
        else:  # 理论上不可达（已过滤 0），保留 mode 兜底
            side = "long" if mode.endswith("long") else "short"
        out.append({
            "contract": symbol, "side": side, "size": size, "mode": mode,
            # 持仓身份与定价：峰值跟踪要靠 `entry_price` 判「还是不是同一条持仓」
            # （平掉再开 / 加仓都会改入场价 → 必须重置峰值，否则会拿旧高点当基准
            # 去卡一条新仓）。缺字段时是 0.0，调用方按「判不了身份」保守处理。
            "entry_price": _to_float(p.get("entry_price")),
            "mark_price": _to_float(p.get("mark_price")),
            # 交易所 `size` 字段原值（int）：`|真实| - |字段|` 就是触发单
            # （只收整数张）必然留下的**保护缺口**，供留痕/告警用。
            "size_field": _num(p.get("size")),
        })
    return out


def _is_order_gone(e: Exception) -> bool:
    """订单在交易所**确实已经不存在**（已撤 / 已成交被清理 / 超过保留期）。

    为什么要跟「接口抖动」分开：`ensure_grid_tp` 的两处 `get_order` 原先把**所有**
    异常都当「查不到 → 不动（fail-closed）」，而 Gate 对已死订单回的就是
    `404 ORDER_NOT_FOUND`（实测 2026-10-16：死档查回来全是 404）—— 于是死档永远清不掉：
      · ⓪ pending 档：`最老优先 + 每轮预算` → 同一批死档每轮吃满预算，**表只增不减**
        （实盘：200 档 / 177 pending，而盘上只有 10 张单）；
      · ① protected 档：永久卡在 protected，而候选只取 pending/filled
        → **该档永远不再补挂止盈**。

    `status` 缺失/为 0（非 HTTP 错误）或 5xx/超时 → 返回 False，仍走 fail-closed
    （宁可漏清，不可误删）。
    """
    if getattr(e, "status", 0) == 404:
        return True
    msg = str(e).upper()
    return "ORDER_NOT_FOUND" in msg or "ORDER_NOT_EXIST" in msg


# 触发单终态：撤旧/统计时这些状态不再操作（同 gateway price_orders 的 status 枚举）。
_TERMINAL_PO_STATUS = frozenset({
    "cancelled", "finished", "filled", "triggered", "failed", "closed",
})

# 执行优先级：数字越小越先执行。**先释放风险、再承担风险**。
_INTENT_PRIORITY = {
    # 1) 平仓：立刻释放保证金与风险
    "close": 1, "close_all": 1, "flatten": 1, "close_long": 1, "close_short": 1,
    # 2) 减仓：部分释放
    "reduce": 2, "reduce_long": 2, "reduce_short": 2,
    # 3) 撤单：腾出挂单额度（Gate 每合约有挂单上限）
    "cancel_all": 3, "cancel_price_all": 3, "cancel_trail_all": 3,
    # 4) 改保护：调整已有仓的保护，不改敞口
    "modify_tp_sl": 4,
    # 5) 开仓/加仓/突破进场：最后做
    "open_long": 5, "open_short": 5, "add_long": 5, "add_short": 5,
    "stop_entry_long": 5, "stop_entry_short": 5,
    # 9) 观望
    "hold": 9,
}

# 「管理类」动作：动仓 / 动保护，但**不动挂单簿**（与 _INTENT_PRIORITY 的 1/2/4 类同源）。
# 用于 `_cancel_stale_owned` 的空窗守卫：只管理、不铺单的轮次里，replace 撤挂单簿
# 没有任何承接物 → 一个轮次节拍的网格空窗（产线实测 2026-10-14，martin-grid 2 天
# 16 次、每次 15~21min）。不含 open/add/stop_entry（那是铺单）与 cancel_*（那是显式
# 撤单，单独走 explicit_cancel_symbols）。
_MGMT_ONLY_ACTIONS = frozenset({
    "modify_tp_sl", "reduce", "reduce_long", "reduce_short",
    "close", "close_all", "close_long", "close_short", "flatten",
})

#: 「铺单类」动作（入场挂单）。用于 2026-10-28 的**缩梯防护**：
#: `replace: symbol` 触发 stale 清理时，只撤与本轮新梯档位重合的旧入场单。
_ENTRY_ACTIONS = frozenset({
    "open_long", "open_short", "add_long", "add_short",
    "stop_entry_long", "stop_entry_short",
})

#: 每轮 `ensure_grid_tp` 最多做多少次「台账 ↔ 交易所对齐」的 `get_order` 查询。
#: 为什么要限：档位表历史上只增不清（实测 5 小时涨到 110 条），若一次全查会把
#: 一轮扫描拖死。老档优先、每轮 20 条 → 病态表约半小时收敛，之后每轮只有新增几条。
_LADDER_PRUNE_MAX = 20


def _int_or_zero(v: Any) -> int:
    """容错取整：非数字/空/None → 0。

    **刻意不用 try/except**：本项目审计 B12 是「裸吞异常棘轮」，为解析加 except
    会被记成新增的吞（executor.py 基线 28）。校验式解析同样安全且不触棘轮。
    """
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if not s:
        return 0
    neg = s.startswith("-")
    body = s[1:] if neg else s
    if body.isdigit():
        return -int(body) if neg else int(body)
    head = body.split(".", 1)[0]
    if head.isdigit():                     # 允许 "1.0" / "1.9" → 1
        return -int(head) if neg else int(head)
    return 0


def _skip_placeholder_tp(account_risk: dict, meta: dict) -> bool:
    """网格腿是否**跳过占位 TP**（2026-10-15，见 `_open` 内详细注释）。

    真值条件 = ① `account_risk.grid_tp_auto` 真会挂单（live/true/on）
              ② 该腿是**网格腿**（`meta.tp_target` 存在；马丁腿不填）。
    只在 live 时成立：dry/off 下若也跳过，档位就彻底没有止盈了。
    """
    gta = str((account_risk or {}).get("grid_tp_auto", "off")).lower()
    return gta in ("live", "true", "on") and bool((meta or {}).get("tp_target"))


def _sort_intents_by_priority(intents: list) -> list:
    """按「先平仓 → 减仓 → 撤单 → 改保护 → 开仓」排序。

    参照 nofx 的 `sortDecisionsByPriority`（close=1 / open=2 / hold=3）。
    稳定排序：同优先级保持信号里的原顺序，所以不会打乱同一动作的多腿。
    未知动作排 6（在开仓之后、hold 之前）—— 宁可晚做，不要抢在平仓前面。
    """
    return sorted(
        intents,
        key=lambda i: _INTENT_PRIORITY.get(str(getattr(i, "action", "") or "").lower(), 6),
    )


# ── 持仓管理闸门（manage_positions / allow_close / allow_reduce）──────────
# 2026-10-13 账户归属事故的第二层防线：引擎按账户净持仓管理，而账户里可能
# 躺着用户手动开的仓（order_scope 只过滤订单、不过滤持仓）。根治靠账户隔离，
# 这里让「平仓/减仓/改保护」变成显式可关的配置（「只开仓、不管仓」模式）。
#
# 别名映射必须按 **requested_action** 判：expand_signal 把
# reduce/reduce_long/reduce_short 统一落成 action="close"、flatten 落成
# action="close_all"（原动作记在 meta.requested_action）—— 只按 action 判
# 会让 allow_reduce=false 被 action="close" 悄悄放行。
_MGMT_CLOSE_ACTS = {"close", "close_all", "flatten"}
_MGMT_REDUCE_ACTS = {"reduce", "reduce_long", "reduce_short"}
# 只受 manage_positions 总闸管辖、不受 allow_close/allow_reduce 影响：
# 它们改的是**保护单价位**，不动敞口。
_MGMT_PROTECT_ACTS = {"trail", "modify_tp_sl"}
_MGMT_ACTS = _MGMT_CLOSE_ACTS | _MGMT_REDUCE_ACTS | _MGMT_PROTECT_ACTS


class Executor:
    def __init__(
        self,
        client: GateClient,
        symbols_whitelist: Optional[list[str]] = None,
        max_notional_usd: Optional[float] = None,
        position_policy: str = "free",
        default_replace: str = "none",
        order_scope: str = "own",
        manage_positions: bool = True,
        allow_close: bool = True,
        allow_reduce: bool = True,
        require_sl: bool = True,
        account_risk: Optional[dict] = None,
        label_prefix: str = "",
        alert_store: Optional[Any] = None,
        root: Optional[Path] = None,
        bot_id: str = "",
        account: str = "",
        place_before_cancel: bool = False,
        ladder_shrink_guard: bool = False,
        ladder_template: Optional[dict] = None,
    ):
        self.client = client
        self.symbols_whitelist = (
            {resolve_symbol(s) for s in symbols_whitelist} if symbols_whitelist else None
        )
        self.max_notional_usd = max_notional_usd
        self.position_policy = (position_policy or "free").lower()
        self.default_replace = (default_replace or "none").lower()
        # own = only cancel this bot's orders (text prefix t-{label}); all = legacy wipe
        self.order_scope = (order_scope or "own").lower()
        # 持仓管理闸门（config.py 同名字段的执行侧；默认全放行）。
        # 为什么必须在 executor：watcher 有 7 处 Executor 构造点、三条 sweep
        # （auto_protect / peak_trail / give_back）**绕过 _execute_intent** 直接
        # 动持仓/保护单 —— 闸门做在方法内部才能全覆盖（做在 execute_signal 入口
        # 会漏掉 sweep，本仓反复出现「闸门测试全绿而生产路径没接线」）。
        self.manage_positions = bool(manage_positions)
        self.allow_close = bool(allow_close)
        self.allow_reduce = bool(allow_reduce)
        # pre-trade: open_* must carry sl (unless explicitly disabled)
        self.require_sl = bool(require_sl)
        # account-level risk: halt / max_total_notional_usd / daily_loss_limit_usd / max_leverage
        self.account_risk = dict(account_risk or {})
        # bot 隔离：日初权益等状态写 data/bots/<bot_id>/state/（勿用共享目录）
        self.root = Path(root) if root is not None else Path.cwd()
        self.bot_id = str(bot_id or "").strip()
        # 所属账户（config/accounts.yaml 的 key）。非空时**日初权益走账户级共享路径**，
        # 这样多个 bot 共账户时能形成**账户级**日亏熔断，而不是各算各的日初
        # （架构盘点 S6：日初权益按 bot 落盘 → 同账户下各自独立触发，不是账户级阈值）。
        self.account = str(account or "").strip()
        # bot namespace: order texts always under t-{label_prefix}*; signal labels cannot escape
        self.label_prefix = str(label_prefix or "").strip()
        # place-before-cancel：显式撤类 intent 转换为「快照差集」语义（config 逐 bot 开启）
        self.place_before_cancel = bool(place_before_cancel)
        # 缩梯防护（2026-10-28）：replace 时若本轮腿数 < 盘上入场单数，
        # 保留价格不重合的旧档（避免阶梯从 10 层塌到 3 层）。默认关，
        # 既有 replace 契约不变（test_replace_no_empty_grid）。
        self.ladder_shrink_guard = bool(ladder_shrink_guard)
        # BOT 级阶梯模板（引擎补齐）。全局默认空 = 不启用。
        self.ladder_template = dict(ladder_template or {})
        # P0.4 告警落盘：权益偏离 / 孤儿保护单
        self.alert_store = alert_store
        # 本轮**将要被撤掉的旧普通单 id 集**（`execute_signal` 每轮重算；见 `_sl_cover_cap`）。
        # 只被 `_open` → `_resolve_sl_size` → `_sl_cover_cap` 读取，而它只在**轮内**被调用，
        # 所以即使跨轮残留也不会被别的路径读到（撤掉的旧 id 也不会再出现在挂单簿里）。
        self._round_replaced_order_ids: set = set()
        # 保证金模式 ensure 的一次性短路标记：(symbol, mode)。
        # Gate 只允许「该合约无持仓、无挂单」时切换（实测 2026-10-16：盘上 10 档
        # 挂单时报 `ORDER_PENDING`），所以失败是常态而非错误 —— 成功过一次即短路，
        # 避免每轮开仓都白跑一次 API（阶梯常态有挂单）。
        self._mm_done: Optional[tuple] = None
        # 节流：`_open` 每轮被调 N 次（N = 档数），而切换失败是常态
        # （有挂单报 ORDER_PENDING）→ 不做节流就是 N 次/轮的无效 API 调用。
        self._mm_last_key: Optional[tuple] = None
        self._mm_last_try: float = 0.0

    #: 保证金模式 ensure 的重试间隔（秒）。阶梯 15 分钟一轮，取 5 分钟 →
    #  一轮最多试 1~2 次，空窗（重铺/清场）一到就能接住。
    _MM_RETRY_SEC = 300.0

    def _ensure_margin_mode(self, symbol: str, want: str) -> str:
        """把合约的保证金模式 ensure 成 `want`（幂等；切不动就静默等空窗）。

        与 `intent.margin_mode` 的关系：**AI 显式给了就以 AI 为准**，否则用
        bot 配置 `account_risk.margin_mode`。配置驱动是必要的 —— 实测人格虽
        写着 `margin_mode: cross`，但字段表与 chip 契约都没有该字段，AI 从
        未写过 → 账户一直是逐仓（`pos_margin_mode: isolated`），
        「配了但没人读」的典型。

        返回 `"cached" | "switched" | "pending" | "throttled" | ""`
        （空 = 值非法，不动；throttled = 距上次尝试不足 `_MM_RETRY_SEC`）。
        """
        want = str(want or "").strip().lower()
        if want not in ("cross", "isolated"):
            return ""
        if self._mm_done == (symbol, want):
            return "cached"
        now = time.time()
        if (self._mm_last_key == (symbol, want)
                and (now - self._mm_last_try) < self._MM_RETRY_SEC):
            return "throttled"
        self._mm_last_key = (symbol, want)
        self._mm_last_try = now
        try:
            self.client.set_margin_mode(symbol, want)
        except Exception as e:  # noqa: BLE001
            # ORDER_PENDING（有挂单/持仓）是预期路径，不是故障 → DEBUG 不刷屏。
            swallow("executor.ensure_margin_mode", e, level=logging.DEBUG)
            return "pending"
        self._mm_done = (symbol, want)
        return "switched"

    def execute_signal(self, signal: SignalFile) -> ExecReport:
        report = ExecReport()
        intents = expand_signal(signal)
        if not intents:
            report.results.append(
                StepResult("hold", "", True, detail={"skipped": True})
            )
            return report

        # ---- D 组合风险闸门（account_risk.total_risk_pct，默认关，逐 bot）----
        # 单档 _align_size_to_risk 只保证「每档 ≤ 权益×risk%÷dist」，挡不住
        # 「每档都合规、10 档加起来爆表」（martin-grid 满阶 29 张止损 ≈40% 权益）。
        # 组合口径：Σ(size_usd × |entry-sl|/entry) > equity × total_risk_pct 时，
        # 全部 open 类等比缩小。触发必打 WARNING（P0 盲区教训）。
        if self.account_risk.get("total_risk_pct"):
            intents, budget_note = self._cap_combined_risk(intents)
            if budget_note:
                log.warning("RISK_BUDGET %s", budget_note)

        # ---- P1 空窗修复（place_before_cancel，逐 bot 开启，默认关）----
        # 显式撤类 intent（cancel_all 等）按优先级排最前先执行 → 「撤完旧的、新的
        # 还没挂上」的交易所空窗（实测 martin-grid 每轮 ~4 分钟无本 bot 挂单）。
        # 开启本开关时，撤类 intent **不单独执行**，转换为「快照差集」语义：
        # replace_mode 提升为 symbol，由末尾 _cancel_stale_owned 在新单挂完后
        # 撤「快照里有、新 plan 里没有」的旧单 —— 撤的是旧单差集，不会误撤新单。
        # 快照面必须覆盖撤类 intent 触及的 symbol（否则撤单语义丢失）→ 用原始 intents。
        converted_cancels: list = []
        need_all_snapshot = False
        if self.place_before_cancel and intents:
            cancel_set = {a for a, p in _INTENT_PRIORITY.items() if p == 3}
            cancels = [i for i in intents
                       if str(getattr(i, "action", "") or "").lower() in cancel_set]
            if cancels and len(cancels) < len(intents):
                snapshot_intents = intents
                intents = [i for i in intents
                           if str(getattr(i, "action", "") or "").lower() not in cancel_set]
                converted_cancels = cancels
                # symbol 为空的撤类（= 全合约）→ 快照面必须扩到全部
                need_all_snapshot = any(
                    not str(getattr(c, "symbol", "") or "").strip() for c in cancels
                )

        replace_mode = self._replace_mode(signal, intents)
        if converted_cancels:
            # 撤类被转换 → 必须激活 stale 清理；cancel_all 的语义 ≈ replace=symbol
            if replace_mode == "none":
                replace_mode = "all" if need_all_snapshot else "symbol"
        pre_owned = self._snapshot_owned(
            snapshot_intents if converted_cancels else intents, replace_mode
        )
        # ── 本轮「将被撤掉的旧普通单」预登记（2026-10-14，修 SL 超配）──────────
        # `_cancel_stale_owned` 的语义是「撤掉本轮开始前快照里的**全部** owned 普通单」
        # （`old_o` 整体撤，只有 reduce_only 保护单会被 `_should_keep_protection` 留下，
        # 而 `_should_keep_protection` 对非 reduce_only 一律返回 False）→ 本轮结束后
        # **旧梯一张不剩**。而 `place_before_cancel`（先挂后撤）期间新旧梯同时在场，
        # `_sl_cover_cap` 会把旧梯也计入敞口 → 归并出来的 SL 张数 = 持仓 + 旧梯 + 新梯
        # ＝**多算一整梯**（产线实测：持仓 5 + 挂单 26 → SL 57 而非 31）。
        # 这里把旧梯 id 预登记下来，供 `_sl_cover_cap` 排除 —— 只在**本轮确实要撤旧**
        # （replace_mode != none）时才登记：replace=none 的轮次旧梯会留着，不能排除。
        self._round_replaced_order_ids = (
            set().union(*(
                (slot.get("order_ids") or set()) for slot in pre_owned.values()
            ))
            if replace_mode != "none" and pre_owned
            else set()
        )

        # 审计留痕：撤类转换必须可见（否则复盘「这轮为什么没撤单」时无从归因）
        for ci in converted_cancels:
            report.results.append(StepResult(
                str(getattr(ci, "action", "cancel")) or "cancel",
                str(getattr(ci, "symbol", "") or ""),
                True,
                detail={"converted": "place_before_cancel",
                        "deferred_to": "stale_cleanup"},
            ))

        # 排序：**先平仓、后开仓**（参照 nofx 的 `sortDecisionsByPriority`）。
        # 先释放保证金再开新仓 —— 否则同轮换仓会因保证金不足被拒，或两笔叠加
        # 撞上总量闸门。`sorted` 是稳定排序，同优先级保持信号里的原顺序。
        intents = _sort_intents_by_priority(intents)

        # ── 模板独占铺梯（2026-10-28）：`ladder_template.enabled` 时，模型的
        # 入场腿一律不执行 —— 由 `_complete_ladder` 按模板铺 10 层。
        # 为什么不「剪枝」就够了：模型每轮锚点不同（实测 2352→2357→2360），
        # 按价格剪枝只能剪掉当轮不重合的，下一轮又铺新的 → 13→12→11→9→15 反复。
        # 两个 master 永远不收敛；阶梯策略里档位本就该固定，交给引擎。
        tpl_intents = list(intents)      # 模板补齐要看**原始**意图（含入场腿的 sl）
        if (getattr(self, "ladder_template", None) or {}).get("enabled"):
            kept = []
            for it in intents:
                if str(getattr(it, "action", "") or "").lower() in _ENTRY_ACTIONS:
                    report.results.append(StepResult(
                        it.action, str(getattr(it, "symbol", "") or ""), True,
                        detail={"skipped": "template_owns_entries",
                                "price": getattr(it, "price", None)}))
                else:
                    kept.append(it)
            intents = kept

        for intent in intents:
            # 优雅停机检查点：参照 nofx 的 `isRunning`（每轮开始 + **每条决策
            # 执行前**都查）。我们原先只有进程级锁 —— 长批次执行中无法立即停手。
            # 触发方式：创建 `data/bots/<id>/state/stop` 文件。
            if self._stop_requested():
                report.results.append(StepResult(
                    intent.action, intent.symbol, False,
                    error="STOP_REQUESTED: 收到停机请求，中止剩余 intent"))
                break
            gate = self._entry_gate(intent)
            if gate:
                map_note = self._map_open_to_add(intent)
                if map_note:
                    # 不能映射（超暴露上限 / 不同侧 / 无持仓）→ **良性跳过**，不再 break：
                    #  · 算整轮失败会白烧周期并污染失败归档（模拟盘历史 73 笔 POSITION_EXISTS）
                    #  · break 会让同一信号里**后面的 intent 全不执行** —— 例如
                    #    [stop_entry_long, modify_tp_sl] 里第一个被拒，合法的「改保护」也丢了
                    report.results.append(
                        StepResult(intent.action, intent.symbol, True,
                                   detail={"gate_skipped": gate, "map_skipped": map_note})
                    )
                    continue
                # 已映射成同侧 add_*（_entry_gate 对 add_* 放行）→ 继续执行
            try:
                step = self._execute_intent(intent)
            except GateApiError as e:
                step = StepResult(intent.action, intent.symbol, False, error=str(e))
            except Exception as e:  # noqa: BLE001 — boundary
                step = StepResult(intent.action, intent.symbol, False, error=repr(e))
            requested = (intent.meta or {}).get("requested_action")
            if requested and requested != step.action:
                extra = {"executed_as": step.action}
                if (intent.meta or {}).get("mapped_from"):
                    # 显式留痕：这条原本是 open_*，被映射成 add_* 执行了
                    # （复盘「为什么这轮加了仓」时要看得到）
                    extra["mapped_from"] = intent.meta["mapped_from"]
                step = StepResult(
                    action=requested,
                    symbol=step.symbol,
                    ok=step.ok,
                    detail={**step.detail, **extra},
                    error=step.error,
                )
            report.results.append(step)
            if not step.ok:
                break

        # 整轮失败 → 回滚本轮新挂的单，不留半成品（见 _rollback_newly_placed）
        if not report.ok:
            self._rollback_newly_placed(pre_owned, intents, report)
            # 回滚后**补跑 stale 清理**（2026-10-27）：原来只有 report.ok 才撤旧单，
            # 失败轮次快照里的旧单（上一轮的阶梯 + SL）永远没人收 —— 失败连续出现时
            # 每轮泄漏一批（paper 实测 20 条入场 + 11 条 SL 堆积）。回滚已撤掉本轮
            # 新单，旧单成了纯垃圾；承接物守卫此时必然 no_replacement，但「整轮失败
            # 回滚」本身就是撤旧依据 —— 把快照面全量当显式撤单面传入。真在保护持仓
            # 的 reduce-only 单仍由 _should_keep_protection 拦住，不会裸奔。
            if replace_mode != "none" and pre_owned:
                self._cancel_stale_owned(
                    pre_owned, intents, report,
                    explicit_cancel_symbols=set(pre_owned),
                )

        # After successful steps: withdraw OLD owned orders (keep newly placed ids)
        elif replace_mode != "none":
            # A 修复（系统级网格空窗）：把「本轮有显式撤单意图的 symbol」一并传下去，
            # 供 _cancel_stale_owned 判断「撤旧单到底有没有承接物」（详见该函数）。
            # converted_cancels 是被 place_before_cancel 转换掉的 cancel_* —— 它们的
            # 撤单语义已落到 stale 清理里，不能被「无承接物」守卫误杀。
            cancel_syms = {
                str(getattr(c, "symbol", "") or "") for c in converted_cancels
            }
            self._cancel_stale_owned(
                pre_owned, intents, report, explicit_cancel_symbols=cancel_syms,
            )
        # BOT 级阶梯补齐（ladder_template.enabled 才生效；默认关 → 行为不变）
        # 用 `tpl_intents`（过滤前）：入场腿虽被跳过执行，但它的 sl 是补齐要用的止损价。
        try:
            self._complete_ladder(tpl_intents, report)
        except Exception as e:  # noqa: BLE001 — 补齐失败绝不影响本轮已成交的腿
            log.warning("ladder template completion failed: %s", e)
        return report

    def _replace_mode(self, signal: SignalFile, intents: list) -> str:
        modes = {getattr(i, "replace", "none") for i in intents}
        if signal.replace and signal.replace != "none":
            modes.add(signal.replace)
        if self.default_replace and self.default_replace != "none":
            modes.add(self.default_replace)
        return "all" if "all" in modes else ("symbol" if "symbol" in modes else "none")

    def _own_namespace(self, label: str = "") -> tuple[str, str]:
        """本 bot 的归属命名空间 → `(text_prefix, tag)`；判不出来时是 `("", "")`。

        `_own_scope_tag`/`_own_prefix` 的**唯一入口**：`label_prefix` 优先（bot 级命名空间，
        signal 的 label 逃不出去），否则用 label 本身。两者都空 = **归属判不了** ——
        调用方必须 fail-closed（跳过 / 拒绝），**不得退化为整表**。
        """
        tag = self._own_scope_tag(label)
        return self._text_prefix(tag), tag

    def _wide_scope(self, scope: str, action: str) -> str:
        """`symbol` 为空时解析作用面；**未显式声明就拒绝**。

        为什么必须拒绝：动作的作用域不该由「参数是否为空」隐式决定 —— 原先
        `close_all`/`cancel_*` 在 `symbol=""` 时静默放大到**全账户**（且 `close_all`
        连归属过滤都没有，会平掉别的 bot 的仓）。能力不丢：显式写 `scope: account`
        仍是全账户语义。单币路径不受影响 —— 它们调这些动作都带 symbol。
        """
        s = str(scope or "").strip().lower()
        if s in ("bot", "account"):
            return s
        if s == "symbol":
            raise GateApiError(
                f"{action}: scope=symbol 需要 symbol（只动一个币就写 symbol）")
        if s:
            raise GateApiError(
                f"{action}: scope 只能是 symbol|bot|account，收到 {scope!r}")
        raise GateApiError(
            f"{action}: 未给 symbol 时必须显式声明作用面 —— "
            f"全账户请写 scope: account，全 bot（本 bot 的 symbols）请写 scope: bot；"
            f"只动一个币请写 symbol")

    def _wide_symbols(self, scope: str, candidates: list) -> list[str]:
        """宽作用面（`symbol` 为空）下本次动作触及的 symbol 列表。

        `scope=bot` → 限定在本 bot 声明的宇宙（`symbols_whitelist`）；
        `scope=account` → 候选集合原样（调用方给的是「账户上一切有仓/有单的合约」）。
        候选集合本身由调用方按动作给（平仓看有仓的合约，撤单看有单的合约）。
        """
        cand = sorted({str(s) for s in (candidates or []) if s})
        if scope == "bot" and self.symbols_whitelist:
            return [s for s in cand if s in self.symbols_whitelist]
        return cand

    def _symbol_owned(self, symbol: str) -> bool:
        """本 bot 在该合约上**有归属证据**（本 bot 命名空间的挂单 / 条件单 text）。

        持仓记录本身不带 text，所以归属只能从订单判：该合约上存在一条 text 落在本 bot
        命名空间（`t-<label_prefix>*`）的单，就认为这个仓是本 bot 的。

        **白名单不算归属**：`symbols_whitelist` 说的是「本 bot 允许交易哪些币」，
        不等于「账户上这个仓是我的」—— 同一个账户上别的 bot 完全可能持有一个也在本
        bot 白名单里的币。拿它当归属证据会把别人的仓平掉（那是这条判据要防的事）。

        **查不到（无本 bot 单 / 接口失败）一律 False = 判不了归属** —— 调用方
        fail-closed（不平它），并**显式报告**（不能静默 no-op，见 `_close_all`）。
        """
        prefix = self._own_prefix("")
        if not prefix:
            return False
        return bool(self._owned_open_orders(symbol, prefix)
                    or self._owned_price_orders(symbol, prefix))

    def _all_owned_symbols(self) -> list[str]:
        """本 bot 在**哪些合约上有挂单/条件单**（`replace=all` 的候选面）。

        只用于把 `replace=all` 与 `replace=symbol` 真正区分开：symbol 只看本轮 payload
        触及的币，all 还要收掉本 bot 在**其他币**上的旧单（`_cancel_stale_owned` 的
        own 分支逐 slot 处理，所以候选面扩在这里）。归属仍靠订单 text，无 bot 命名空间
        时返回空集（fail-closed，不放大到整表）。
        """
        prefix, label = self._own_namespace("")
        if not prefix or not label:
            return []
        out: set[str] = set()
        try:
            for o in self.client.list_orders() or []:
                sym = o.get("contract")
                if sym and self._text_owned(str(o.get("text") or ""), label):
                    out.add(str(sym))
        except GateApiError:
            pass
        try:
            for p in self.client.list_price_orders(None) or []:
                sym = p.get("contract") or (p.get("initial") or {}).get("contract")
                text = str((p.get("initial") or {}).get("text") or p.get("text") or "")
                if sym and self._text_owned(text, label):
                    out.add(str(sym))
        except GateApiError:
            pass
        return sorted(out)

    def _text_prefix(self, label: str) -> str:
        return f"t-{label}" if label else ""

    def _bot_tag(self, label: str) -> str:
        """Ownership tag. When label_prefix is set, signal labels cannot escape the bot namespace."""
        label = (label or "signal").strip()
        if self.label_prefix:
            if label == self.label_prefix or label.startswith(self.label_prefix + "-"):
                return label
            return f"{self.label_prefix}-{label}"
        return label

    def _own_scope_tag(self, label: str) -> str:
        """Tag used to filter cancel/replace under order_scope=own."""
        if self.label_prefix:
            return self.label_prefix
        return (label or "").strip()

    def _own_prefix(self, label: str = "") -> str:
        """text prefix used for own-scope ownership filtering."""
        tag = self._own_scope_tag(label)
        return self._text_prefix(tag)

    def _text_owned(self, text: str, label: str) -> bool:
        """Segment-safe ownership: t-{label} or t-{label}-*, never t-{label}XX."""
        if not label:
            return False
        text = str(text or "")
        tag = f"t-{label}"
        return text == tag or text.startswith(tag + "-")

    def _owned_price_orders(self, symbol: str, prefix: str) -> list[dict]:
        if not prefix:
            return []
        label = prefix[2:] if prefix.startswith("t-") else prefix
        try:
            rows = self.client.list_price_orders(symbol) or []
        except GateApiError:
            return []
        out = []
        for p in rows:
            text = str((p.get("initial") or {}).get("text") or p.get("text") or "")
            if self._text_owned(text, label):
                out.append(p)
        return out

    def _owned_open_orders(self, symbol: str, prefix: str) -> list[dict]:
        if not prefix:
            return []
        label = prefix[2:] if prefix.startswith("t-") else prefix
        try:
            rows = self.client.list_orders(symbol) or []
        except GateApiError:
            return []
        return [o for o in rows if self._text_owned(str(o.get("text") or ""), label)]

    def _rollback_newly_placed(self, pre_owned: dict, intents: list, report: ExecReport) -> None:
        """整轮失败时撤掉**本轮新挂出去**的单，不留半成品。

        为什么需要：主循环里任一腿失败就 `break`，而**之前成功的腿已经挂在交易所上了**。
        对阶梯策略尤其危险 —— 11 档挂出 5 档就断，留下一个缺腿的网格
        （双向缺一侧 = 对冲不成立），而且 `report.ok=False` 会让「先挂后撤」那步
        也不执行，等于**新旧叠加**。实测 2026-10-06 发生过两次：一次挂出前 4 条多腿
        就被 SSL 掐断、一次只挂出第 1 条腿。

        **判据：本轮新挂的 = 当前 owned − 开始前快照**。用集合差而不是解析
        `step.detail` —— 各 action 的 detail 结构不同（`order` / `tp_orders` /
        `sl_orders` / …），逐个解析必然漏，而漏掉的就是留在场上的垃圾。

        **绝不碰两样东西**：
        1. 快照里就存在的单（上一轮挂的，不归本轮管）；
        2. **正在保护真实持仓的 reduce-only 单** —— 若某条腿已成交，撤掉它的
           止损会留下**裸仓**，那比半成品严重得多。判据复用 `_live_protection_ids`
           （与孤儿扫描同源）。持仓取不到时**整段跳过撤价单**，宁可不回滚也不冒裸仓风险。
        """
        # 先撤未成交的入场单（它们还在 list_orders(status=open) 里；已成交的不会出现）
        cancelled = 0
        rolled_legs: list[dict] = []
        for intent in intents:
            if not intent.symbol:
                continue
            prefix = self._own_prefix(intent.label)
            if not prefix:
                continue
            before = (pre_owned or {}).get(intent.symbol) or {}
            pre_o = {str(x) for x in (before.get("order_ids") or ())}
            for o in self._owned_open_orders(intent.symbol, prefix):
                oid = str(o.get("id") or "")
                if not oid or oid in pre_o:
                    continue
                try:
                    self.client.cancel_order(oid)
                    cancelled += 1
                    rolled_legs.append({"symbol": intent.symbol, "kind": "order", "id": oid})
                except GateApiError as e:
                    report.results.append(StepResult(
                        "rollback", intent.symbol, False,
                        detail={"cancel_order": oid}, error=str(e)[:120]))

        # 再撤保护单：跳过正在保护真实持仓的那些
        positions, perr = self._positions_or_error()
        if positions is None:
            report.results.append(StepResult(
                "rollback", "", False,
                detail={"refused": "positions_unavailable"},
                error=f"持仓查询失败，跳过保护单回滚（无法区分预挂保护与持仓保护）: {perr}"))
        else:
            for intent in intents:
                if not intent.symbol:
                    continue
                prefix = self._own_prefix(intent.label)
                if not prefix:
                    continue
                before = (pre_owned or {}).get(intent.symbol) or {}
                pre_p = {str(x) for x in (before.get("price_ids") or ())}
                rows = self._owned_price_orders(intent.symbol, prefix)
                live = self._live_protection_ids(rows, positions)
                for p in rows:
                    pid = str(self._order_id(p) or "")
                    if not pid or pid in pre_p or pid in live:
                        continue
                    try:
                        self.client.cancel_price_order(pid)
                        cancelled += 1
                        rolled_legs.append({"symbol": intent.symbol, "kind": "price", "id": pid})
                    except GateApiError as e:
                        report.results.append(StepResult(
                            "rollback", intent.symbol, False,
                            detail={"cancel_price_order": pid}, error=str(e)[:120]))

        # 落**明细**：谁被回滚了 —— 通知要靠它把「挂单成功」卡改成「已回滚」卡
        # （否则通知与最终状态自相矛盾：单已经被撤了，卡片还在说挂单成功）。
        report.results.append(StepResult(
            "rollback", "", True,
            detail={
                "rolled_back": cancelled,
                "rolled_back_symbols": sorted({leg["symbol"] for leg in rolled_legs}),
                "rolled_back_legs": rolled_legs,
                "reason": "本轮有腿失败，已撤掉本轮新挂的单",
            }))

    def _snapshot_owned(self, intents: list, mode: str = "none") -> dict:
        """{symbol: {"prefix": str, "price_ids": set, "order_ids": set}}

        `mode="all"`（`replace=all`）时把快照面扩到本 bot 有单的**全部**合约 ——
        否则 all 与 symbol 行为完全相同（审计 C-5）。`symbol` 模式只看本轮 payload
        触及的币（行为不变）。
        """
        pairs: list[tuple[str, str]] = []
        for intent in intents:
            if not intent.symbol:
                continue
            prefix = self._own_prefix(intent.label)
            if prefix:
                pairs.append((intent.symbol, prefix))
        if mode == "all":
            # payload 之外的合约：归属只能按 bot 命名空间判（`_all_owned_symbols` 无
            # 命名空间时返回空集 → 不放大）
            bot_prefix = self._own_prefix("")
            if bot_prefix:
                pairs += [(sym, bot_prefix) for sym in self._all_owned_symbols()]
        snap: dict[str, dict] = {}
        for symbol, prefix in pairs:
            slot = snap.setdefault(symbol, {"prefix": prefix, "price_ids": set(), "order_ids": set()})
            for p in self._owned_price_orders(symbol, prefix):
                pid = self._order_id(p)
                if pid:
                    slot["price_ids"].add(pid)
            for o in self._owned_open_orders(symbol, prefix):
                oid = str(o.get("id") or "")
                if oid:
                    slot["order_ids"].add(oid)
        return snap

    def _cancel_stale_owned(
        self, pre_owned: dict, intents: list, report: ExecReport,
        explicit_cancel_symbols: Optional[set] = None,
    ) -> None:
        """Cancel old owned orders that are not part of the new plan (place-before-cancel).

        `explicit_cancel_symbols`：本轮带显式撤单意图（被 place_before_cancel 转换的
        cancel_*）的 symbol 集合；空串 "" 代表全合约撤单意图。用于「无承接物守卫」——
        详见下方 owned 路径的 A 修复说明。
        """
        if self.order_scope == "all":
            # legacy wipe after new plan is in place
            positions, perr = self._positions_or_error()
            if positions is None:
                report.results.append(StepResult(
                    "replace_cancel", "", False,
                    detail={"refused": "positions_unavailable"},
                    error=f"持仓查询失败，拒绝 replace 整表撤单（无法区分保护单与陈旧单）: {perr}"))
                return
            symbols = sorted({i.symbol for i in intents if i.symbol})
            if not symbols and self.symbols_whitelist:
                symbols = sorted(self.symbols_whitelist)
            elif not symbols:
                symbols = self._open_symbols()
            guarded = [s for s in symbols
                       if self._live_protection_ids(self.client.list_price_orders(s) or [], positions)]
            if guarded:
                report.results.append(StepResult(
                    "replace_cancel", "", False,
                    detail={"refused": "live_protection_present", "symbols": guarded},
                    error=f"{guarded} 上有正在保护持仓的 tp/sl，整表撤单会撤掉它们，已拒绝"))
                return
            for sym in symbols:
                try:
                    self.client.cancel_all_orders(sym)
                    self.client.cancel_all_price_orders(sym)
                    report.results.append(StepResult("replace_cancel", sym, True,
                        detail={"replace": "all", "cancelled": ["orders", "price_orders"], "order_scope": "all"}))
                except GateApiError as e:
                    report.results.append(StepResult("replace_cancel", sym, False, error=str(e)))
            return

        # Collect new ids to keep
        keep_price: dict[str, set] = {}
        keep_orders: dict[str, set] = {}
        for intent in intents:
            if not intent.symbol:
                continue
            prefix = self._own_prefix(intent.label)
            keep_price.setdefault(intent.symbol, set())
            keep_orders.setdefault(intent.symbol, set())
            for p in self._owned_price_orders(intent.symbol, prefix):
                keep_price[intent.symbol].add(self._order_id(p))
            for o in self._owned_open_orders(intent.symbol, prefix):
                keep_orders[intent.symbol].add(str(o.get("id") or ""))

        positions, perr = self._positions_or_error()
        if positions is None:
            report.results.append(StepResult(
                "replace_cancel", "", False,
                detail={"refused": "positions_unavailable"},
                error=f"持仓查询失败，拒绝 replace 撤旧单（无法区分保护单与陈旧单）: {perr}"))
            return

        for sym, slot in pre_owned.items():
            prefix = slot.get("prefix") or ""
            # pre_owned was snapshotted BEFORE this run — these are the old ones.
            # Newly placed orders are not in this set, so cancel the snapshot as-is.
            old_p = slot.get("price_ids") or set()
            old_o = slot.get("order_ids") or set()
            # **保护真实持仓的单不是「陈旧单」**：replace 把它们撤掉会让持仓裸奔。
            # 实测 2026-10-04：r12 那条信号带 `replace=owned`，`cancel_price_all`
            # 已按新闸门跳过保护单，但这里照样把 `t-wyk-tp`/`t-wyk-sl` 撤了 ——
            # 同一条语义的三条路径（自动清理 / 显式撤单 / replace 撤旧单）必须同源。
            cur_price = {self._order_id(p): p
                         for p in self._owned_price_orders(sym, prefix)}
            cur_orders = {str(o.get("id") or ""): o
                          for o in self._owned_open_orders(sym, prefix)}
            pending_entry = self._has_pending_entry(sym)
            # **本轮已给这个 symbol 重挂保护单时，旧的「预挂保护单」纯属多余**。
            # 不加这个例外的话，`_should_keep_protection` 会因为「有待成交入场单」
            # （replace 语义下恒为真）把上一轮的孤儿保护单全部保留 → 每轮累积一批 →
            # 实测 8 小时触发 40 次 `orphan_protector` 对账告警。reconcile 每次都能
            # 兜底清干净，但噪音会淹没真问题，且白费撤单调用。
            # 安全性：本函数在**新单挂完之后**才跑（place-before-cancel），所以撤旧时
            # 新保护单已就位；真有持仓对应的保护单仍由 `_should_keep_protection` 拦住。
            fresh_protection = bool(keep_price.get(sym))
            keep_pending = pending_entry and not fresh_protection

            # ── A 修复（系统级网格空窗防护，2026-10-14）─────────────────────
            # 撤旧单必须有「承接物」，否则撤了就是一个轮次节拍的网格空窗。
            # 触发面（**管理轮**）：本轮对该 symbol 有「动仓/动保护但不动挂单簿」的
            #   管理动作（modify_tp_sl / reduce* / close* / flatten），却没挂出新单。
            #   此时 AI 明显是在管理已有持仓，不是重铺挂单簿 —— replace 把整梯撤空
            #   就是 bug（产线实测 martin-grid 2 天 16 次、每次 15~21min 盘上无网格）。
            # 承接物（二选一即可放行撤旧）：
            #   ① 本轮为该 symbol 新挂出的 **普通单**（阶梯/入场挂单）—— 判据用「当前
            #      普通单 id 集」减「本轮开始前的快照 id 集」，有新 id 即本轮挂了新挂单
            #      （place-before-cancel 下新单此刻已就位）。
            #   ② 本轮对该 symbol 的显式撤单意图（converted cancel_*）。
            # ⚠️ 承接物**只认普通单**，绝不认条件单（TP/SL）：管理轮（modify_tp_sl）
            #   本身就会挂一条新 TP，若把它算作承接物，守卫**永不触发** —— 实测
            #   2026-10-14：本轮挂新 TP → placed_new=True → 10 张阶梯照撤、空窗照旧。
            #   阶梯是普通单，它的承接物只能是新普通单。
            # 边界（保持 replace 既有语义，不误伤）：空 intent 轮 / 纯 hold 轮不触发本
            #   守卫 —— replace 的文档本义就是「挂新单前先撤旧单（anti-pile-up）」，
            #   这类轮次在产线不会带非空挂单簿到达此处（hold 无 symbol → 快照为空），
            #   保持原状以免改动作用域语义（test_dull_action_scope / test_cancel_safety
            #   的 replace 作用域与持仓过滤契约）。
            # 本守卫对**所有** trigger replace 的路径生效（含 default_replace=symbol
            # 的配置型触发，与 bridge 的契约层修复互为纵深）。
            placed_new = bool(set(cur_orders) - set(old_o))
            has_cancel_intent = bool(
                explicit_cancel_symbols
                and (sym in explicit_cancel_symbols or "" in explicit_cancel_symbols)
            )
            is_mgmt_round = any(
                str(getattr(i, "symbol", "") or "") in (sym, "")
                and str(getattr(i, "action", "") or "").lower() in _MGMT_ONLY_ACTIONS
                for i in intents
            )
            if is_mgmt_round and not placed_new and not has_cancel_intent:
                report.results.append(StepResult(
                    "replace_cancel", sym, True,
                    detail={"skipped": "no_replacement", "prefix": prefix,
                            "reason": "本轮未为该 symbol 挂新单且无显式撤单，"
                                      "保留旧单避免网格空窗"}))
                continue

            cancelled = []
            skipped = []
            for pid in sorted(old_p):
                if self._should_keep_protection(cur_price.get(str(pid)), positions, keep_pending):
                    skipped.append(("price", pid))
                    continue
                try:
                    self.client.cancel_price_order(pid)
                    cancelled.append(("price", pid))
                except GateApiError:
                    pass
            # ── 缩梯防护（2026-10-28 实测事故）─────────────────────────────
            # `replace: symbol` 会把「不在本轮计划里」的旧入场单全撤。实测 19:29 轮
            # 模型只规划 3 条腿（1 网格 + 2 马丁），旧 7 条网格档被 stale 清理全撤 →
            # 盘上阶梯从 10 层塌缩到 3 层（TP/SL 尺寸随之漂移）。
            # 规则：**只撤与本轮新梯档位重合（±0.3% ≈ 7 点）的旧入场单**；价格不
            # 重合的档位是独立阶梯档，保留（既不缩梯也不堆叠 —— 同位置被新档替换）。
            # 阈值必须小于最小档距（人格 10 点 ≈ 0.43%）：0.6% 会把相邻档误判成重合。
            new_entry_prices: list[float] = []
            for it in intents:
                if str(getattr(it, "symbol", "") or "") not in (sym, ""):
                    continue
                if str(getattr(it, "action", "") or "").lower() not in _ENTRY_ACTIONS:
                    continue
                try:
                    px = float(getattr(it, "price", None) or 0)
                except (TypeError, ValueError):
                    px = 0.0
                if px > 0:
                    new_entry_prices.append(px)
            # 只在「本轮铺得比盘上少」时启用保留：正常重铺（腿数 ≥ 现有单数）
            # 仍按 replace 契约整梯替换，不改变既有行为（test_replace_no_empty_grid）。
            old_entry_count = sum(
                1 for oid in old_o
                if not (cur_orders.get(str(oid)) or {}).get("is_reduce_only")
            )
            shrink_keep_enabled = (getattr(self, "ladder_shrink_guard", False)
                                   and bool(new_entry_prices)
                                   and len(new_entry_prices) < old_entry_count)
            shrink_kept = []
            for oid in sorted(old_o):
                cur = cur_orders.get(str(oid)) or {}
                if self._should_keep_protection(cur, positions, pending_entry):
                    skipped.append(("order", oid))
                    continue
                try:
                    o_px = float(cur.get("price") or 0)
                except (TypeError, ValueError):
                    o_px = 0.0
                if shrink_keep_enabled and o_px > 0 and not any(
                        abs(o_px - p) / p <= 0.003 for p in new_entry_prices):
                    # 价格不重合 = 独立档位，保留（缩梯防护）
                    shrink_kept.append(oid)
                    continue
                try:
                    self.client.cancel_order(oid)
                    cancelled.append(("order", oid))
                except GateApiError:
                    pass
            if cancelled or skipped or shrink_kept:
                detail = {"replace": "owned", "prefix": prefix,
                          "cancelled": cancelled,
                          "skipped_live_protection": skipped}
                if shrink_kept:
                    detail["ladder_shrink_guarded"] = shrink_kept
                report.results.append(StepResult(
                    "replace_cancel", sym, True, detail=detail,
                ))

    def _complete_ladder(self, intents: list, report) -> None:
        """BOT 级阶梯补齐（`ladder_template.enabled`，全局默认关闭）。

        为什么要有：模型每轮给的入场腿数不稳定（实测 3/4/10 条都出现过），
        而 `replace` 是整梯替换 → 阶梯层数随模型波动，10 层变 3 层。
        补齐只在「模板启用 + 本轮有入场腿（拿得到 sl）+ 存在缺档」时动手：
          · 锚 = 最新价；网格档 = 锚 × (1 − step_pct×i)；
          · 马丁档 = 锚 × (1 − pct)，张数按模板；
          · 已存在（±0.3%）的档跳过 —— **只补缺，不撤不改**；
          · 单次最多补 template 总档数，避免异常配置刷单。
        """
        tpl = getattr(self, "ladder_template", None) or {}
        if not tpl.get("enabled"):
            return
        entry_legs = [i for i in intents
                      if str(getattr(i, "action", "") or "").lower() in _ENTRY_ACTIONS]
        if not entry_legs:
            return
        sym = str(getattr(entry_legs[0], "symbol", "") or "")
        if not sym:
            return
        sl = getattr(entry_legs[0], "sl", None)
        if sl is None:
            return                      # 没有 sl 的轮次不补（补齐必须带止损）
        try:
            mark = float(self.client.get_last_price(sym) or 0)
            meta = self.client.get_contract(sym)
        except Exception as e:  # noqa: BLE001
            report.results.append(StepResult("ladder_template", sym, False,
                                             error=f"取价/合约失败: {e}"))
            return
        if mark <= 0:
            return
        try:
            grid_n = int(tpl.get("grid_levels") or 0)
            grid_lots = float(tpl.get("grid_lots") or 1)
            step_pct = float(tpl.get("grid_step_pct") or 0.6)
            martin = list(tpl.get("martin") or [])
        except (TypeError, ValueError):
            return
        want: list[tuple[float, float]] = []
        for i in range(1, grid_n + 1):
            want.append((mark * (1 - step_pct * i / 100.0), grid_lots))
        for m in martin:
            try:
                want.append((mark * (1 - float(m["pct"]) / 100.0), float(m["lots"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not want:
            return
        # ── 模板权威：撤掉不在模板档位上的入场单（2026-10-28）────────────
        # 模型每轮都会把它自己的 12 条档位铺回来（replace 整梯替换），只靠「补缺 +
        # 上限」无法收敛：实测清理到 10 层后，下一轮又变 12 条（2211.5×9 / 2223.5×1 复活）。
        # 既然这个 bot 的策略就是固定阶梯，模板启用时它就是唯一权威：
        # 价格不在「模板 10 档 ±0.3%」内的入场单撤掉，然后把缺的补上。
        tmpl_prices = [price for price, _ in want]          # want 已按 mark 计算好
        removed = []
        try:
            owned = [o for o in self.client.list_orders(sym) or []
                     if float(o.get("size") or 0) > 0
                     and not (o.get("is_reduce_only") or o.get("reduce_only"))]
        except Exception:  # noqa: BLE001
            owned = []
        for o in owned:
            try:
                px = float(o.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            if any(abs(px - t) / t <= 0.003 for t in tmpl_prices if t > 0):
                continue
            try:
                self.client.cancel_order(o["id"])
                removed.append({"price": px, "size": o.get("size")})
            except Exception as e:  # noqa: BLE001
                report.results.append(StepResult(
                    "ladder_template", sym, False,
                    detail={"price": px}, error=f"撤非模板档失败: {str(e)[:80]}"))
        if removed:
            report.results.append(StepResult(
                "ladder_template", sym, True,
                detail={"pruned_off_template": removed, "anchor": mark}))

        # 已存在的入场档（价格）
        try:
            existing = [float(o.get("price") or 0)
                        for o in self.client.list_orders(sym) or []
                        if not (o.get("is_reduce_only") or o.get("reduce_only"))]
        except Exception:  # noqa: BLE001
            existing = []
        # **总层数上限**（2026-10-28 实测）：模型自己铺的档位与模板价不重合 →
        # 模板只会「补缺」，两套叠加会把 10 层变成 14 层（实测 martin-grid
        # 入场单 14 条、马丁 9/9/9）。模板的语义是「保证有 10 层」，不是「再加 10 层」：
        # 现有入场单已经 ≥ 模板总档数时，本轮不补。
        if len(existing) >= len(want):
            report.results.append(StepResult(
                "ladder_template", sym, True,
                detail={"skipped": "already_full", "existing": len(existing),
                        "template_total": len(want)}))
            return
        placed, skipped = [], []
        for px, lots in want:
            try:
                price = round_price(px, meta)
            except Exception:  # noqa: BLE001
                continue
            if any(p > 0 and abs(price - p) / p <= 0.003 for p in existing):
                skipped.append(price)
                continue
            if price >= mark:           # 只补现价下方
                skipped.append(price)
                continue
            if len(existing) >= len(want):
                skipped.append(price)
                continue
            body = {"contract": sym, "size": lots, "price": str(price),
                    "tif": "gtc", "text": f"t-{self._bot_tag('')}"}
            try:
                order = self.client.place_order(body)
                placed.append({"price": price, "size": lots,
                               "id": str((order or {}).get("id") or "")})
                existing.append(price)
            except Exception as e:  # noqa: BLE001
                report.results.append(StepResult(
                    "ladder_template", sym, False,
                    detail={"price": price, "size": lots},
                    error=f"补档失败: {str(e)[:120]}"))
        # ── 同价去重（2026-10-28）：模型与本补档在同一轮抢挂同一价（实测 2247×2）
        # 快照取单发生在本轮下单之前，模型随后挂出的档看不到 → 同价重复。
        # 按 ±0.3% 分桶，每桶只留一条（优先张数更接近模板的），其余撤掉。
        dup_removed = []
        try:
            books = [o for o in self.client.list_orders(sym) or []
                     if float(o.get("size") or 0) > 0
                     and not (o.get("is_reduce_only") or o.get("reduce_only"))]
            buckets: list[list[dict]] = []
            for o in sorted(books, key=lambda x: -float(x.get("price") or 0)):
                px = float(o.get("price") or 0)
                for b in buckets:
                    if abs(px - float(b[0].get("price") or 0)) / max(px, 1e-9) <= 0.003:
                        b.append(o)
                        break
                else:
                    buckets.append([o])
            for b in buckets:
                if len(b) < 2:
                    continue
                b.sort(key=lambda x: -float(x.get("size") or 0))   # 保留张数大的
                for o in b[1:]:
                    try:
                        self.client.cancel_order(o["id"])
                        dup_removed.append({"price": float(o.get("price") or 0),
                                            "size": o.get("size")})
                    except Exception as e:  # noqa: BLE001
                        report.results.append(StepResult(
                            "ladder_template", sym, False,
                            detail={"price": o.get("price")},
                            error=f"去重撤单失败: {str(e)[:80]}"))
        except Exception as e:  # noqa: BLE001
            log.warning("ladder dedupe failed %s: %s", sym, e)

        if placed or skipped or dup_removed:
            report.results.append(StepResult(
                "ladder_template", sym, True,
                detail={"placed": placed, "skipped": skipped, "dup_removed": dup_removed,
                        "anchor": mark, "sl": sl}))

    def _entry_gate(self, intent: Intent) -> str:
        """Return error string to reject intent, or '' to allow.

        position_policy:
          free        — always allow
          manage_only — with position on symbol: allow manage (add/reduce/close/cancel/hold/trail),
                        reject new entry (open_*/stop_entry_*)
          strict      — like manage_only; opposite-side add_* also rejected
        """
        policy = self.position_policy
        if policy not in ("strict", "manage_only"):
            return ""
        action = intent.action
        manage = {
            "hold", "modify_tp_sl", "close", "close_all", "flatten",
            "cancel_all", "cancel_price_all", "cancel_trail_all",
            "reduce", "reduce_long", "reduce_short", "trail",
        }
        # normalized after alias map: open_*, stop_entry_*, add_* via requested
        requested = (intent.meta or {}).get("requested_action") or action
        if action in manage or requested in manage:
            return ""
        if action == "hold":
            return ""
        # entry-like: open_long/open_short/stop_entry_*
        pos = self._symbol_positions(intent.symbol)
        if not pos:
            return ""
        # allow same-side add_* only
        if requested in ("add_long", "open_long") and all(p["side"] == "long" for p in pos):
            if requested == "add_long":
                return ""
            # open_long with existing long = new plan pile-up → reject under both
            return f"POSITION_EXISTS: {intent.symbol} already has position; use add_long/reduce or replace plan first"
        if requested in ("add_short", "open_short") and all(p["side"] == "short" for p in pos):
            if requested == "add_short":
                return ""
            return f"POSITION_EXISTS: {intent.symbol} already has position; use add_short/reduce or replace plan first"
        if policy == "strict" and requested in ("add_long", "add_short"):
            return f"POSITION_POLICY_STRICT: opposite add not allowed while position open on {intent.symbol}"
        return (
            f"POSITION_EXISTS: {intent.symbol} has open position "
            f"({'/'.join(sorted({p['side'] for p in pos}))}); "
            f"policy={policy} only allows add/reduce/close/tp-sl"
        )

    def _map_open_to_add(self, intent: Intent) -> str:
        """有持仓时的**同侧** `open_*` → 视为 `add_*`。返回 '' 表示已映射，否则返回不映射的原因。

        动机：AI 说 `open_long` 而已有同侧持仓时，`_entry_gate` 会拒（注释写明防
        「new plan pile-up」），但 AI 的意图往往是「想更长」—— 拒掉等于白烧一轮
        （实测 `POSITION_EXISTS` 占修复后残留失败的 28%）。

        **必须带暴露上限**：映射成 add 意味着重复/过期计划会**每轮都加仓**，在杠杆下就是
        仓位膨胀（而且单调累积、不会自己回退）。所以只在「当前持仓名义 + 本单名义 ≤
        权益 × `max_notional_pct`」时才映射 —— 用账户自己的暴露策略当上限；超出就退回拒绝，
        防堆积保护重新生效。
        """
        requested = (intent.meta or {}).get("requested_action") or intent.action
        try:
            pos = self._symbol_positions(intent.symbol)
        except Exception:  # noqa: BLE001
            return "无法读取持仓"
        if not pos:
            return "无持仓"
        sides = {p["side"] for p in pos}
        if len(sides) != 1:
            return f"持仓方向不唯一 {sorted(sides)}"
        side = next(iter(sides))
        if requested == f"add_{side}":
            return ""                                   # 本来就是 add
        if requested != f"open_{side}":
            return f"与持仓不同侧（{requested} vs {side}）—— 应先 close/reduce"
        try:
            pct = float((self.account_risk or {}).get("max_notional_pct") or 5.0)
            acct = self.client.get_account() or {}
            equity = float(acct.get("total") or acct.get("balance") or 0)
            meta = self.client.get_contract(intent.symbol)
            quanto = float(getattr(meta, "quanto_multiplier", 0) or 0)
            px = float(self.client.get_last_price(intent.symbol) or 0)
            cur = abs(sum(float(p["size"]) for p in pos)) * quanto * px
            add = intent.size_usd
            if add is None and intent.size is not None:
                add = abs(float(intent.size)) * quanto * px
            add = float(add or 0)
            if equity > 0 and pct > 0 and quanto > 0 and px > 0:
                if (cur + add) > equity * pct:
                    return (f"加仓后名义 {cur + add:.0f} > 权益×{pct:g}={equity * pct:.0f}"
                            "（防仓位膨胀）")
        except Exception:  # noqa: BLE001 — 算不出上限就不映射（保守）
            return "暴露上限无法计算"
        intent.meta = {**(intent.meta or {}),
                       "requested_action": f"add_{side}",
                       "mapped_from": intent.action}
        return ""

    def _symbol_positions(self, symbol: str) -> list[dict]:
        try:
            positions = self.client.get_positions() or []
        except Exception as e:  # noqa: BLE001
            # 关键路径：账户取数失败被吞成空列表时，调用方会看到「无持仓」而不是
            # 「取数失败」—— 这正是 2026-10-13 事故那类误判的温床。留痕 + 计数。
            swallow("executor._symbol_positions", e, level=logging.WARNING)
            return []
        # 合约元数据用于把 `size`（int64）反算成真实张数（见 `position_qty`）。
        return _parse_symbol_positions(positions, symbol, meta=self._safe_meta(symbol))

    def _safe_meta(self, symbol: str):
        """合约元数据；取不到返回 None（调用方退回 `size` 字段的旧口径）。

        `get_contract` 有进程内 + 磁盘缓存，热路径上不额外打网络。
        """
        try:
            return self.client.get_contract(symbol)
        except Exception as e:  # noqa: BLE001
            swallow("executor._safe_meta", e, level=logging.DEBUG)
            return None

    def _management_gate(self, action: str, intent: Optional[Intent] = None) -> str:
        """持仓管理动作闸门 → 拒绝原因（"" = 放行）。

        判据用 **requested_action**（expand_signal 的别名原动作）回退到
        action：reduce_* 落成 close、flatten 落成 close_all，只按 action
        判会让 allow_reduce=false 被 close 悄悄放行。

        返回的文案以机器可读代号开头（MANAGE_POSITIONS_OFF / CLOSE_DISABLED /
        REDUCE_DISABLED），供调用方落 `gate_skipped` 与复盘归因。
        """
        requested = ""
        if intent is not None:
            requested = str((intent.meta or {}).get("requested_action") or "").strip().lower()
        eff = requested or str(action or "").strip().lower()
        if eff not in _MGMT_ACTS:
            return ""
        if not self.manage_positions:
            return ("MANAGE_POSITIONS_OFF: manage_positions=false，本 bot 只开仓、"
                    "不管仓（禁一切持仓管理动作）")
        if eff in _MGMT_CLOSE_ACTS and not self.allow_close:
            return f"CLOSE_DISABLED: allow_close=false，禁止平仓动作 {eff}"
        if eff in _MGMT_REDUCE_ACTS and not self.allow_reduce:
            return f"REDUCE_DISABLED: allow_reduce=false，禁止减仓动作 {eff}"
        return ""

    def _execute_intent(self, intent: Intent) -> StepResult:
        print(self.client.banner())
        action = intent.action
        # 持仓管理闸门（manage_positions / allow_close / allow_reduce）。
        # 良性跳过：ok=True + gate_skipped —— 与 `_entry_gate` 的映射跳过同口径。
        # 不能返回 ok=False：主循环见 False 会 break 并触发 _rollback_newly_placed
        # （把同一信号里已挂好的新单撤掉），一个策略性禁用不该产生那种副作用。
        gate = self._management_gate(action, intent)
        if gate:
            log.warning("MGMT_GATE %s %s: %s", action, intent.symbol, gate)
            return StepResult(action, intent.symbol, True,
                              detail={"gate_skipped": gate})
        # `scope` 必须一路传到钝动作：`symbol=""` 时它决定作用面（bot/account）。
        # 不传 = 永远命中「未显式声明 → 拒绝」分支（能力实现了，但生产路径接不上线 ——
        # 本仓踩过：闸门测试全绿而生产路径根本没接线）。
        scope = getattr(intent, "scope", "") or ""
        if action == "hold":
            return StepResult("hold", "", True, detail={"skipped": True})
        if action == "modify_tp_sl":
            return self._modify_tp_sl(intent)
        if action == "close_all":
            return self._close_all(intent.symbol, scope)
        if action == "cancel_all":
            return self._cancel_all(intent.symbol, intent.label or "", scope)
        if action == "cancel_price_all":
            return self._cancel_price_all(intent.symbol, intent.label or "", scope)
        if action in ("stop_entry_long", "stop_entry_short"):
            return self._stop_entry(intent)
        if action == "close":
            return self._close(intent)
        if action == "trail":
            return self._trail(intent)
        if action == "cancel_trail_all":
            return self._cancel_trail_all(intent.symbol, scope)
        if action in ("open_long", "open_short"):
            return self._open(intent)
        return StepResult(action, intent.symbol, False, error=f"unhandled action {action}")

    # ── guards ───────────────────────────────────────────
    def _check_symbol(self, symbol: str) -> None:
        if not symbol:
            raise GateApiError("symbol required")
        if self.symbols_whitelist is not None and symbol not in self.symbols_whitelist:
            raise GateApiError(f"symbol {symbol} not in whitelist {sorted(self.symbols_whitelist)}")

    def _check_open_sl(self, intent: Intent) -> None:
        """Reject naked entries: open_* / stop_entry_* must set sl."""
        if not self.require_sl:
            return
        action = (intent.meta or {}).get("requested_action") or intent.action
        if action not in ("open_long", "open_short", "stop_entry_long", "stop_entry_short"):
            return
        if intent.sl is None:
            raise GateApiError(
                "SL_REQUIRED: open/stop_entry requires sl (or set require_sl: false)"
            )

    def _auto_halt_path(self) -> Path:
        """自动熔断标记的位置：有 account 走账户级（跨 bot 共享），否则 bot 级。"""
        if self.account:
            return (Path(self.root) / "data" / "accounts" / self.account
                    / "state" / "halt.json")
        return (Path(self.root) / "data" / "bots" / (self.bot_id or "_unknown")
                / "state" / "halt.json")

    def _read_auto_halt(self) -> str:
        """读自动熔断标记，返回原因（空 = 未熔断）。

        **按 UTC 日自动复位**：标记里记了 `day`，跨日即视为失效 —— 所以
        「日亏熔断」不需要任何定时任务来解除。人工 `halt: true` 仍然独立生效
        （两者在 `_check_account_risk` 里取逻辑或）。
        """
        try:
            import json as _json

            p = self._auto_halt_path()
            if not p.exists():
                return ""
            rec = _json.loads(p.read_text(encoding="utf-8"))
            if not rec.get("halt"):
                return ""
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            if str(rec.get("day") or "") != today:
                return ""
            return str(rec.get("reason") or "halt")
        except Exception:  # noqa: BLE001 — 读不到就当未熔断，保持原行为
            return ""

    def _write_auto_halt(self, reason: str) -> None:
        """置位自动熔断（当日有效，跨日自动失效）。"""
        try:
            import json as _json
            import time as _time
            from datetime import datetime, timezone

            p = self._auto_halt_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps({
                "halt": True,
                "reason": reason,
                "day": datetime.now(timezone.utc).strftime("%Y%m%d"),
                "ts": int(_time.time()),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _check_equity_deviation_halt(self, start: float, total: float) -> str:
        """权益**向下**偏离日初超阈值 → 自动熔断（逐 bot 配置，默认关）。

        与 `daily_loss_limit_usd` 的分工：
          - `daily_loss_limit_usd` 是**绝对值**（亏 $20 停手），小资金账户配不出
            有意义的数（$20 对 $84 权益是 24%），只能配成「几乎不触发」。
          - 本闸门是**比例**（亏掉日初的 X% 停手），小账户也能表达「亏一成停手」。

        背景（架构盘点 S18）：`equity_deviation` 告警此前**只落盘、无人消费** ——
        本项目调研的结论是「告警不接自动动作 = 没有控制」。这里把告警接上动作：
        写 `halt.json`（当日有效、跨日自动复位），之后所有开仓类动作被
        `_check_account_risk` 挡住，只留 close/cancel。

        `equity_deviation_halt` 三种取值与 `auto_protect` 一致：
          false（默认）→ 不启用，行为与升级前完全一致
          "dry"        → 只记日志「本来会熔断」，不写标记（观察期用）
          true         → 写熔断标记

        **只在向下偏离时触发**：向上偏离 10% 是盈利，不该停手。

        返回熔断原因（未触发/未启用返回空串）。
        """
        ar = self.account_risk or {}
        mode = ar.get("equity_deviation_halt", False)
        if not mode:
            return ""
        try:
            pct = float(ar.get("equity_deviation_halt_pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct <= 0 or start <= 0:
            return ""
        dev = (total - start) / start * 100.0
        if dev > -pct:
            return ""
        reason = (f"equity_deviation {dev:+.1f}% <= -{pct:g}% "
                  f"(day_start={start:.2f} now={total:.2f})")
        if str(mode).lower() == "dry":
            log.warning("[dry] would auto-halt: %s", reason)
            return ""
        self._write_auto_halt(reason)
        log.error("auto halt: %s", reason)
        if self.alert_store is not None:
            try:
                from .monitoring.alerts import TYPE_EQUITY_HALT

                self.alert_store.raise_alert(
                    TYPE_EQUITY_HALT, reason,
                    deviation_pct=round(dev, 2), threshold_pct=pct,
                    day_start=start, now=total,
                )
            except Exception:  # noqa: BLE001
                pass
        return reason

    def _check_account_risk(self, intent: Intent) -> None:
        """Account-level halt / exposure / daily-loss limits (pre-trade, all configurable)."""
        ar = self.account_risk or {}
        action = (intent.meta or {}).get("requested_action") or intent.action
        # 文案带 symbol：账户级闸门被触发时归档顶层的 error 不带币就**无法归因**
        # （多币下一个账户里同时挂着几个币的单）。约定与 `_check_notional` 同源：
        # 错误码后紧跟 `<symbol> `，symbol 为空时不加任何字符（单币自动补路径恒非空）。
        sym = str(getattr(intent, "symbol", "") or "").strip()
        where = f"{sym} " if sym else ""
        manage = {"hold", "modify_tp_sl", "close", "close_all", "flatten", "cancel_all", "cancel_price_all",
                  "cancel_trail_all", "reduce", "reduce_long", "reduce_short"}
        # 人工 halt（yaml）与**自动熔断**（日亏触发、跨日自动复位）取逻辑或。
        # 原先 halt 只能人工改 yaml —— 出了事要有人在场才停得下来。
        auto_halt = self._read_auto_halt()
        if (ar.get("halt") or auto_halt) and action not in manage:
            raise GateApiError(
                "HALTED: " + where
                + ("account_risk.halt=true" if ar.get("halt") else f"auto halt ({auto_halt})")
                + "; only close/cancel allowed")
        if action not in manage and action.startswith(("open", "add", "stop_entry")):
            # daily loss vs day-start equity (strategy-adjustable)
            dlimit = ar.get("daily_loss_limit_usd")
            if dlimit is not None:
                try:
                    total = float((self.client.get_account() or {}).get("total") or 0)
                    start = self._day_start_equity(total)
                    if start > 0 and (start - total) > float(dlimit):
                        # 落盘**自动熔断标记**：否则「日亏超限」只在有开仓意图时
                        # 才拦，而 AI 改说 close/modify 就绕过去了 —— 下一轮再提
                        # 开仓时又要重新算一遍（且日初权益可能已被别的 bot 改写）。
                        # 置位后所有开仓类动作一律被 halt 挡住，跨日自动复位。
                        self._write_auto_halt(
                            f"daily_loss_limit {start - total:.2f} > {dlimit}")
                        raise GateApiError(
                            f"DAILY_LOSS_LIMIT: {where}loss={start - total:.2f} > {dlimit} "
                            f"(day_start={start:.2f} now={total:.2f})"
                        )
                except GateApiError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
            # P0.4：权益偏离告警（不拦截，只落盘）+ 可选的**自动熔断**（架构盘点 S18）。
            # 原先这条告警**只落盘、无人消费** —— 本项目调研结论是「告警不接自动
            # 动作 = 没有控制」。现在同一处算出的偏离值也喂给
            # `_check_equity_deviation_halt`：逐 bot 配置、默认关，
            # 开了就写 halt.json（当日有效、跨日自动复位）。
            try:
                acct = self.client.get_account() or {}
                total = float(acct.get("total") or acct.get("balance") or 0)
                start = self._day_start_equity(total)
                if self.alert_store is not None:
                    self.alert_store.equity_deviation(start, total)
                self._check_equity_deviation_halt(start, total)
            except Exception:  # noqa: BLE001
                pass
            max_lev = ar.get("max_leverage")
            if max_lev is not None and intent.leverage and int(intent.leverage) > int(max_lev):
                raise GateApiError(
                    f"MAX_LEVERAGE: {where}{intent.leverage} > account max_leverage={max_lev}"
                )
            # total notional: sum open positions + this order
            # 两个闸门取更小者：绝对值 max_total_notional_usd 与**按比例**的
            # max_total_notional_pct（权益 × pct）。后者权益变化时自动跟随 —— 绝对值
            # 做不到这点（实测实盘 max_total_notional_usd=10000 相当于 113× 权益，永不触发，
            # 真正 bind 的只有杠杆上限 50×；加了 open_*→add_* 映射后仓位会单调增长，
            # 所以必须有一道**随权益收紧**的总量闸门）。
            max_total = ar.get("max_total_notional_usd")
            max_total_pct = ar.get("max_total_notional_pct")
            if max_total is not None or max_total_pct is not None:
                try:
                    pos_notional = 0.0
                    for p in self.client.get_positions() or []:
                        # 局部量不能叫 qty（会遮蔽模块级 qty() → UnboundLocalError）
                        pos_q = _num(p.get("size"))
                        if pos_q == 0:
                            continue
                        mark = float(p.get("mark_price") or p.get("entry_price") or 0)
                        mult = 1.0
                        try:
                            mult = float(self.client.get_contract(p.get("contract")).quanto_multiplier or 1)
                        except Exception:  # noqa: BLE001
                            mult = 1.0
                        pos_notional += mark * abs(pos_q) * mult
                    this_usd = intent.size_usd
                    if this_usd is None and intent.size is not None:
                        px = intent.price or self.client.get_last_price(intent.symbol)
                        try:
                            mult = float(self.client.get_contract(intent.symbol).quanto_multiplier or 1)
                        except Exception:  # noqa: BLE001
                            mult = 1.0
                        this_usd = abs(intent.size) * float(px) * mult
                    cap = float(max_total) if max_total is not None else None
                    pct_note = ""
                    if max_total_pct is not None:
                        eq = float((self.client.get_account() or {}).get("total") or 0)
                        if eq <= 0:
                            raise GateApiError(
                                f"MAX_TOTAL_NOTIONAL: {where}cannot read equity for max_total_notional_pct"
                            )
                        pc = eq * float(max_total_pct)
                        cap = pc if cap is None else min(cap, pc)
                        pct_note = f" (权益×{float(max_total_pct):g})"
                    if cap is None:
                        raise GateApiError(f"MAX_TOTAL_NOTIONAL: {where}no cap configured")
                    if this_usd is not None and pos_notional + float(this_usd) > cap:
                        raise GateApiError(
                            f"MAX_TOTAL_NOTIONAL: {where}open={pos_notional:.2f} + this={this_usd:.2f} "
                            f"> {cap:.0f}{pct_note}"
                        )
                except GateApiError:
                    raise
                except Exception:  # noqa: BLE001 — if cannot measure, do not silently allow huge size
                    raise GateApiError(f"MAX_TOTAL_NOTIONAL: {where}cannot measure account exposure")

    def _day_start_equity(self, current_total: float) -> float:
        """Persist UTC-day start equity for daily_loss_limit.

        **按 bot 隔离**：写 data/bots/<bot_id>/state/_account_risk/equity_<day>.json。
        所有 bot 共享一个文件会让 DAILY_LOSS_LIMIT 拿别家的日初对比自家权益。
        """
        from datetime import datetime, timezone
        import json as _json

        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if self.account:
            # 账户级：多个 bot 共享同一份日初权益 → 日亏熔断是**账户级**的
            base = Path(self.root) / "data" / "accounts" / self.account / "state"
        else:
            try:
                from .paths import bot_paths

                base = bot_paths(self.root, self.bot_id or "_unknown").state / "_account_risk"
            except Exception:  # noqa: BLE001
                base = Path(self.root) / "data" / "bots" / (self.bot_id or "_unknown") / "state" / "_account_risk"
        path = base / f"equity_{day}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                data = _json.loads(path.read_text(encoding="utf-8"))
                return float(data.get("start_equity") or 0)
            path.write_text(_json.dumps({"start_equity": current_total}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            return current_total
        return current_total

    # ── 尺寸四口径：唯一入口 ──────────────────────────────────────────────
    # 模型只可能给这四种之一（`schema.py` 的 `_SIZE_FIELDS`）：
    #   size       张数      —— 精确意图，不可执行就出声（绝不静默放大）
    #   size_usd   名义 USDT —— 系统换算
    #   size_pct   可用权益比例（0..1）
    #   margin_pct 保证金比例（0..1，× 杠杆得名义）
    # 同时给多个 = 模型自相矛盾 → 拒单（此前是静默「size 优先」，另一个口径白写）。
    # 六个口径，与交易所下单面板的自然输入对齐（2026-10-28 补后两个）：
    #   size        张数（合约数，最精确，模型自己算）
    #   size_coins  币数量（0.138 ETH / 0.5 SOL）→ 张 = 币 / quanto
    #   size_usd    名义 USDT（= 币 × 价）
    #   margin_usd  指定保证金 USDT → 名义 = margin × leverage
    #   size_pct    可用 USDT 的百分比 → 名义
    #   margin_pct  可用 USDT 的百分比 × 杠杆 → 名义
    _SIZE_BASES = ("size", "size_usd", "size_coins", "margin_usd",
                   "size_pct", "margin_pct")

    def _resolve_order_size(
        self,
        intent: Intent,
        meta,
        entry_hint: Optional[float] = None,
    ) -> tuple[float, float, str]:
        """把意图解析成 **(张数, 名义 USDT, 口径)**，张数恒为正（方向由 action 决定）。

        价格来源优先级：`entry_hint` > `intent.price` > 最新成交价。只有名义口径
        需要价格；显式张数路径**不打网络**（此前 `_open` 也只在 else 分支取价，保持）。
        """
        given = [b for b in self._SIZE_BASES if getattr(intent, b, None) is not None]
        if not given:
            raise GateApiError(
                "SIZE_REQUIRED: 未给 size / size_coins / size_usd / margin_usd / "
                "size_pct / margin_pct 任一口径")
        if len(given) > 1:
            raise GateApiError(
                f"SIZE_CONFLICT: 同时给了 {given} —— 只允许一个口径，"
                "否则其中一个会被静默忽略")
        basis = given[0]
        symbol = getattr(intent, "symbol", "")
        mult = float(getattr(meta, "quanto_multiplier", 0) or 0)

        if basis == "size":
            raw = abs(float(intent.size))
            if raw <= 0:
                raise GateApiError(f"SIZE_INVALID: {symbol} size=0 无法下单")
            step = safe_step(meta)
            q = quantize_size(raw, meta, mode="floor", at_least_step=False)
            if q <= 0:
                size_min = float(getattr(meta, "size_min", 0) or 0) or step
                raise GateApiError(
                    f"SIZE_TOO_SMALL: {symbol} size={raw:g} 小于最小可下量 {size_min:g}"
                    f"（步长 {step:g}）—— 拒绝静默放大")
            notional = q * (entry_hint or intent.price or 0) * mult if mult > 0 else 0.0
            if not notional:
                px = intent.price or self.client.get_last_price(symbol)
                notional = q * float(px or 0) * mult
            return q, notional, basis

        if basis == "size_coins":
            # 币数量 → 张数：张 = 币 ÷ quanto（与价格无关）。按步长 **floor**，
            # 宁可少下也不超出指定币量（超出等于未经授权的敞口）。
            coins = abs(float(intent.size_coins))
            if mult <= 0:
                raise GateApiError(
                    f"SIZE_INVALID: {symbol} 缺 quanto_multiplier，无法把 {coins:g} 枚换成张数")
            raw = coins / mult
            step = safe_step(meta)
            q = float(floor_to_step(raw, step))
            if q <= 0:
                size_min = float(getattr(meta, "size_min", 0) or 0) or step
                raise GateApiError(
                    f"SIZE_TOO_SMALL: {symbol} size_coins={coins:g} 折 {raw:g} 张，"
                    f"小于最小可下量 {size_min:g} 张（币口径最小单位 "
                    f"{getattr(meta, 'min_size_coins', 0):g}）")
            # 审计：币数量口径按步长 floor 会少下一个最小单位（PEPE 一个单位
            # = 1,000,000 枚 ≈ 4U，SOL 是 0.1 枚）。差额必须留痕，否则「我说 0.55
            # 结果只下了 0.5」无法事后归因。
            got = q * mult
            if got < coins - 1e-12:
                log.info(
                    "size_coins=%.8g → 实下 %.8g 枚（步长 %.4g 张，少 %.8g 枚 < 1 个最小单位 %.8g）",
                    coins, got, step, coins - got, float(getattr(meta, "min_size_coins", 0) or 0))
            px = entry_hint or intent.price
            if not px:
                px = self.client.get_last_price(symbol)
            return q, q * float(px) * mult, basis

        entry = entry_hint or intent.price
        if entry is None:
            entry = self.client.get_last_price(symbol)
        if not entry or float(entry) <= 0:
            raise GateApiError(f"SIZE_INVALID: {symbol} 无可用入场价，无法按名义换算张数")
        entry = float(entry)

        if basis == "size_usd":
            size_usd = float(intent.size_usd)
        elif basis == "margin_usd":
            # 指定保证金：名义 = 保证金 × 杠杆（杠杆缺省取 1，退化成名义口径）。
            # 保证金口径的语义是「用多少钱」，不是「开多大仓」——必须乘杠杆。
            lev = int(intent.leverage or 0) or 1
            size_usd = float(intent.margin_usd) * lev
        elif basis == "size_pct":
            size_usd = pct_to_size_usd(float(intent.size_pct), self.client.get_available_usdt())
        else:
            size_usd = (pct_to_size_usd(float(intent.margin_pct),
                                        self.client.get_available_usdt())
                        * int(intent.leverage or 1))
        q = usd_to_contracts(size_usd, entry, meta)
        return float(q), float(q) * entry * mult, basis

    def _check_notional(self, size_usd: Optional[float], symbol: str = "") -> None:
        if size_usd is None:
            return
        # 权益比例硬顶：默认单笔名义 ≤ 5×权益（可配 account_risk.max_notional_pct）
        # 注意：紧止损 + 2% 风险会推高名义；0.5× 会误杀正常仓（永续带杠杆）
        # 文案带 symbol：多币下归档顶层的 error 不带币就**无法归因**（I6）
        where = f"{symbol} " if symbol else ""
        pct = self.account_risk.get("max_notional_pct")
        if pct is None:
            pct = 5.0
        try:
            pct = float(pct)
            if pct > 0:
                acct = self.client.get_account() or {}
                equity = float(acct.get("total") or acct.get("balance") or 0)
                if equity > 0:
                    cap_pct = equity * pct
                    if size_usd > cap_pct:
                        raise GateApiError(
                            f"MAX_NOTIONAL_PCT: {where}size_usd={size_usd:.0f} "
                            f"> equity×{pct:.0%}={cap_pct:.0f}"
                        )
        except GateApiError:
            raise
        except Exception:  # noqa: BLE001
            pass
        if self.max_notional_usd is not None and size_usd > self.max_notional_usd:
            raise GateApiError(
                f"{where}size_usd={size_usd} exceeds max_notional_usd={self.max_notional_usd}"
            )

    def _cap_combined_risk(self, intents: list) -> tuple[list, str]:
        """组合止损上限：Σ(size_usd × dist) > equity × total_risk_pct 时全部 open 等比缩。

        与单档 `_align_size_to_risk` 的分工：单档管「每笔 ≤ 权益×risk%÷dist」，
        本方法管「所有 open 档加起来的止损合计」。只对 size_usd 型 + 带 sl + 有
        entry 的 open/stop_entry 生效（张数型/无 sl 的交给各自闸门）。
        权益拿不到 → 原样返回（fail-open，不 block 下单——它只缩仓不拒单）。
        """
        try:
            pct = float(self.account_risk.get("total_risk_pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct <= 0:
            return intents, ""
        open_acts = {"open_long", "open_short", "stop_entry_long", "stop_entry_short"}
        risked = []
        for i in intents:
            if str(getattr(i, "action", "") or "").lower() not in open_acts:
                continue
            sz = getattr(i, "size_usd", None)
            sl = getattr(i, "sl", None)
            entry = getattr(i, "price", None)
            if sz is None or sl is None or not entry:
                continue
            try:
                dist = abs(float(entry) - float(sl)) / float(entry)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if dist <= 0:
                continue
            risked.append((i, dist * float(sz)))
        if not risked:
            return intents, ""
        try:
            acct = self.client.get_account() or {}
            equity = float(acct.get("total") or acct.get("balance") or 0)
        except Exception:  # noqa: BLE001
            return intents, ""
        if equity <= 0:
            return intents, ""
        limit = equity * pct
        total = sum(r[1] for r in risked)
        if total <= limit:
            return intents, ""
        scale = limit / total

        # ── 最小可交易单位保护（2026-10-28 实测事故）────────────────────
        # 等比缩放会把「网格档 1 张」缩成 0.92 张 → floor 到步长 0.9 张 →
        # Gate `orders` 端点对 ETH 实际按**整数张** floor → **0 张挂单**（实测
        # 19:53 轮 7 条 size=0 垃圾单，全来自这里）。
        # 1 张已是最小可交易单位、无法再小，缩放它只会产生 0 张单：
        #   · 折算张数 ≤1 的腿**跳过缩放**（保持 1 张）；
        #   · 只缩 >1 张的腿，并按「缩放后仍 ≥1 张」为下限。
        # 代价：极端情况下组合风险略超 cap，但 cap 是软上限、0 张单是硬故障。
        meta_cache: dict[str, object] = {}

        def _contracts(intent) -> float:
            sym = str(getattr(intent, "symbol", "") or "")
            if sym not in meta_cache:
                try:
                    meta_cache[sym] = self.client.get_contract(sym)
                except Exception:  # noqa: BLE001
                    meta_cache[sym] = None
            meta = meta_cache[sym]
            try:
                mult = float(getattr(meta, "quanto_multiplier", 0) or 0)
                px = float(getattr(intent, "price", 0) or 0)
                usd = float(getattr(intent, "size_usd", 0) or 0)
                if mult > 0 and px > 0 and usd > 0:
                    return usd / (px * mult)
            except (TypeError, ValueError):
                pass
            return 0.0

        scalable, skipped_small = [], []
        for i, risk in risked:
            (scalable if _contracts(i) > 1.05 else skipped_small).append((i, risk))
        if not scalable:
            note = (f"combined stop-loss {total:.0f}U > equity {equity:.0f}U × {pct:.0%}"
                    f" → {len(risked)} legs all ≤1 张，跳过缩放（最小单位不可缩）")
            return intents, note
        # scale 仍按**全体**风险算（跳过缩放的小腿会略微超出 cap，可接受）
        scale = limit / total if total > 0 else 1.0
        risked_ids = {id(r[0]) for r in scalable}
        out = []
        for i in intents:
            if id(i) in risked_ids:
                try:
                    before = _contracts(i)
                    i.size_usd = round(float(i.size_usd) * scale, 2)
                    after = _contracts(i)
                    if before > 1.05 and after < 1.0:
                        # 缩到不足 1 张 → 回退到「恰好 1 张」的等价名义
                        i.size_usd = round(float(getattr(i, "price", 0)) * 0.01, 2)
                        log.warning("风险缩放会把 %s 缩到 <1 张（%.2f→%.2f），"
                                    "已回退为 1 张（最小可交易单位）",
                                    i.symbol, before, after)
                except (TypeError, ValueError):
                    pass
            out.append(i)
        note = (f"combined stop-loss {total:.0f}U > equity {equity:.0f}U × {pct:.0%}"
                f" → 缩 {len(scalable)} 腿 ×{scale:.2f}"
                f"（{len(skipped_small)} 条 ≤1 张腿未缩）")
        return out, note

    def _align_size_to_risk(self, intent: Intent, entry: float) -> tuple[float, str]:
        """按风险公式钳制 size_usd：应有 = 权益×risk% ÷ SL距离% × 入场。

        返回 (adjusted_size_usd, note)。无 sl/权益时原样返回。
        """
        size_usd = intent.size_usd
        if size_usd is None or intent.sl is None or entry <= 0:
            return size_usd, ""
        sl = float(intent.sl)
        dist = abs(entry - sl) / entry
        if dist <= 0:
            return size_usd, ""
        risk_pct = self.account_risk.get("risk_pct")
        if risk_pct is None:
            risk_pct = self.account_risk.get("risk_per_trade_pct", 0.01)
        try:
            risk_pct = float(risk_pct)
        except (TypeError, ValueError):
            risk_pct = 0.01
        if risk_pct <= 0:
            return size_usd, ""
        try:
            acct = self.client.get_account() or {}
            equity = float(acct.get("total") or acct.get("balance") or 0)
        except Exception:  # noqa: BLE001
            return size_usd, ""
        if equity <= 0:
            return size_usd, ""
        # dist = |entry-sl|/entry（比例）；应有名义 = 权益×risk% ÷ dist
        expected = equity * risk_pct / dist
        if expected <= 0:
            return size_usd, ""
        # 名义硬顶在这里**钳制**。`_check_notional` 里的 max_notional_usd 是拒单闸门
        # （超了整笔 raise），若只靠它，风险公式的期望值一旦超过上限就会把正常单
        # 整笔拒掉 —— 而「名义上限」的直觉语义是「最多开这么多」。
        # 实测：equity 85 / risk 2% / 止损 0.296% 算出 574，配 max_notional_usd=500
        # 会拒单；钳制后同一张单缩到 500 照常开。
        cap = self.max_notional_usd
        target = expected if cap is None else min(expected, float(cap))
        # 钳制留痕带 symbol：多币下「这轮为什么缩了仓」要能直接归因到币
        where = f"{intent.symbol} " if getattr(intent, "symbol", "") else ""
        # 偏差超过 30% 过大 → 钳到应有值；过小（<50%）保留（可主动降风险）
        if float(size_usd) > target * 1.3:
            note = f"size_clamped {where}{float(size_usd):.0f}->{target:.0f} (risk {risk_pct:.1%} expected)"
            # P0 观测补丁（martin-grid 6h 体检）：note 之前只进 StepResult.detail，
            # 日志里完全看不到 —— 产线 29 张/754U 被钳成 11 张/275U 长期无感知。
            # 钳制改变实际风险敞口，必须进 WARNING 级日志。
            log.warning("RISK_ALIGN %s", note)
            return target, note
        if cap is not None and float(size_usd) > float(cap):
            # 没超过 target×1.3，但仍越过了名义硬顶 → 必须钳，否则 _check_notional 会拒
            return float(cap), f"size_capped {where}{float(size_usd):.0f}->{float(cap):.0f} (max_notional_usd)"
        # 权益比例上限此前**没有钳制路径**，只有 `_check_notional` 的拒单闸门 —— 见
        # `_NOTIONAL_DRIFT_TOL` 的说明。这里只钳微越界。
        pct_cap = self._notional_pct_cap(equity)
        if pct_cap is not None:
            s = float(size_usd)
            if pct_cap < s <= pct_cap * (1 + _NOTIONAL_DRIFT_TOL):
                return pct_cap, (
                    f"size_capped {where}{s:.2f}->{pct_cap:.2f} (max_notional_pct drift)"
                )
        return float(size_usd), ""

    def _notional_pct_cap(self, equity: float) -> Optional[float]:
        """权益比例上限（`account_risk.max_notional_pct`）；未配置或权益无效返回 None。"""
        pct = self.account_risk.get("max_notional_pct")
        try:
            pct = float(pct) if pct is not None else 0.0
        except (TypeError, ValueError):
            return None
        if pct <= 0 or equity <= 0:
            return None
        return equity * pct

    def _atr_pct(self, symbol: str, lookback: int = 14) -> float:
        """近 lookback 根 K 线的 ATR%（=ATR/close×100）。取不到返回 0。"""
        try:
            klines = self.client.get_klines(symbol, "1h", lookback + 1) or []
            if len(klines) < 2:
                return 0.0
            trs = []
            for i in range(1, len(klines)):
                h = float(klines[i].get("h") or klines[i].get("high") or 0)
                l = float(klines[i].get("l") or klines[i].get("low") or 0)
                pc = float(klines[i - 1].get("c") or klines[i - 1].get("close") or 0)
                if h <= 0 or l <= 0:
                    continue
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            if not trs:
                return 0.0
            atr = sum(trs[-lookback:]) / len(trs[-lookback:])
            close = float(klines[-1].get("c") or klines[-1].get("close") or 0)
            if close <= 0:
                return 0.0
            return atr / close * 100.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _vol_adjust(self, intent: Intent, size_usd: float) -> tuple[float, str]:
        """ATR 目标波动缩放（account_risk.vol_target_pct=0 或未配 → 不动）。"""
        target = (self.account_risk or {}).get("vol_target_pct")
        if not target:
            return size_usd, ""
        try:
            target = float(target)
        except (TypeError, ValueError):
            return size_usd, ""
        if target <= 0:
            return size_usd, ""
        atr_pct = self._atr_pct(intent.symbol)
        if atr_pct <= 0:
            return size_usd, ""
        adjusted = vol_adjust_size(float(size_usd), atr_pct, target_pct=target)
        if adjusted == float(size_usd):
            return size_usd, ""
        where = f"{intent.symbol} " if getattr(intent, "symbol", "") else ""
        note = f"vol_adjust {where}{float(size_usd):.0f}->{adjusted:.0f} (atr {atr_pct:.2f}% target {target:.2f}%)"
        return adjusted, note

    # ── actions ──────────────────────────────────────────
    def _exit_trigger_legs(self, intent: Intent) -> list[tuple[str, float, int]]:
        """intent 的全部退出腿 `[(kind, px, rule)]` —— precheck 与 modify 过滤的唯一来源。

        档位清单用 `schema.tp_levels_of()`（唯一来源）：漏一档就等于放一腿非法
        触发价过关。rule 缺省时按**持仓方向**推断，绝不盲猜。
        """
        open_action = "open_long" if (intent.side or "long") == "long" else "open_short"
        legs: list[tuple[str, float, int]] = []
        if intent.tp is not None and intent.tp_mode != "limit_order":
            rule_tp = intent.trigger_rule_tp
            if rule_tp is None:
                rule_tp = infer_trigger_rules(open_action, is_tp=True)
            for px, _share in tp_levels_of(intent):
                legs.append(("tp", float(px), int(rule_tp)))
        if intent.sl is not None and intent.sl_mode != "limit_order":
            rule_sl = intent.trigger_rule_sl
            if rule_sl is None:
                rule_sl = infer_trigger_rules(open_action, is_tp=False)
            legs.append(("sl", float(intent.sl), int(rule_sl)))
        return legs

    def _mark_ref(self, intent: Intent) -> float:
        """参考价（mark/last）；取不到返回 0.0（调用方按「不拦」处理）。"""
        try:
            ticker = self.client.get_ticker(intent.symbol) or {}
            return float(ticker.get("mark_price") or ticker.get("last") or 0)
        except Exception:  # noqa: BLE001 — 取不到参考价就不拦，保持原行为
            return 0.0

    def _precheck_exit_triggers(self, intent: Intent) -> Optional[str]:
        """校验 TP/SL 触发价是否落在参考价（mark）的合法一侧。

        Gate 条件单硬规则：rule=1（涨破触发）要求触发价 > mark，rule=2（跌破触发）
        要求触发价 < mark；否则会立即触发，交易所直接拒（AUTO_TRIGGER_PRICE_GREATE_MARK
        / _LESS_MARK）。而 TP/SL 价位是从**入场价**推出来的：入场价离市价较远时，
        推出来的价位就落在 mark 的非法一侧 —— 那时保护单挂不上，但入场单已经挂在
        交易所上了，等于留下一张**无保护的待成交委托**（一旦成交就是裸仓）。

        所以宁可不交易：任一腿触发价非法就整笔中止，连入场单也不下。
        **入口路径专用**（_open/_stop_entry）；modify 路径走
        `_filter_illegal_exit_legs`（非法腿跳过而非整笔拒）。
        """
        legs = self._exit_trigger_legs(intent)
        if not legs:
            return None
        ref = self._mark_ref(intent)
        if ref <= 0:
            return None
        bad = []
        for kind, px, rule in legs:
            if rule == 1 and px <= ref:
                bad.append(f"{kind}={px:g} 需 > mark {ref:g}（rule=1 涨破触发）")
            elif rule == 2 and px >= ref:
                bad.append(f"{kind}={px:g} 需 < mark {ref:g}（rule=2 跌破触发）")
        # 文案带 symbol：多币下「哪条腿的触发价非法」必须能直接归因到币
        where = f"{intent.symbol} " if getattr(intent, "symbol", "") else ""
        return ("TRIGGER_PRICE_SIDE: " + where + "; ".join(bad)) if bad else None

    def _filter_illegal_exit_legs(
        self, intent: Intent, legs: list[tuple[str, float, int]]
    ) -> tuple[list[tuple[str, float, int]], list[str]]:
        """按 mark 合法一侧过滤退出腿 → `(合法腿, 非法腿描述)`。

        **只给 modify_tp_sl 用**。入口路径保持「一腿非法整笔中止」（未成交挂单
        可以宁可不交易）；modify 面对的是**已成交持仓**，一腿非法就整笔拒的后果
        是「一档中值够不着 → 所有档位永远改不了」——2026-10-15 实盘：网格档
        中值止盈在现价下方，LLM 连续 10+ 轮只能 hold，共用 TP 假网格的成因之一。
        非法腿跳过 + 留痕，合法腿照挂、旧结构中合法部分照撤。
        """
        if not legs:
            return legs, []
        ref = self._mark_ref(intent)
        if ref <= 0:
            return legs, []  # 取不到参考价：不拦（与 precheck 同口径）
        good: list[tuple[str, float, int]] = []
        bad: list[str] = []
        for kind, px, rule in legs:
            if rule == 1 and px <= ref:
                bad.append(f"{kind}={px:g} 需 > mark {ref:g}（rule=1 涨破触发）")
            elif rule == 2 and px >= ref:
                bad.append(f"{kind}={px:g} 需 < mark {ref:g}（rule=2 跌破触发）")
            else:
                good.append((kind, px, rule))
        return good, bad

    @staticmethod
    def _rebuild_tp_levels(
        intent: Intent, kept: list[tuple[float, Optional[float]]]
    ) -> Intent:
        """按 (px, share) 整体重建 intent 的 tp 档位字段（tp/tp2/tp3 + tp_extra/shares）。

        E1 过滤后档位变少，必须整体重建 —— 只改 `tp` 会把 `tp2/tp3` 留下，
        造成「过滤了 tp 却挂出 tp2」的串档。
        """
        intent.tp = float(kept[0][0])
        intent.tp1_share = kept[0][1]
        intent.tp2 = float(kept[1][0]) if len(kept) > 1 else None
        intent.tp2_share = kept[1][1] if len(kept) > 1 else None
        intent.tp3 = float(kept[2][0]) if len(kept) > 2 else None
        intent.tp3_share = kept[2][1] if len(kept) > 2 else None
        intent.tp_extra = [float(px) for px, _ in kept[3:]]
        intent.tp_extra_shares = [sh for _, sh in kept[3:]]
        return intent

    def _stop_requested(self) -> bool:
        """优雅停机：`state/stop` 文件存在时中止执行。

        参照 nofx 的 `isRunning` 检查点（每轮开始 + 每条决策执行前都查）。
        我们原先只有进程级锁 —— 长批次执行中无法立即停手（要么等整批跑完，
        要么 kill 进程留下半完成状态）。
        """
        if not self.bot_id:
            return False
        try:
            return (Path(self.root) / "data" / "bots" / self.bot_id
                    / "state" / "stop").exists()
        except Exception:  # noqa: BLE001
            return False

    def _effective_no_flip(self) -> bool:
        """反手闸门的**生效值** —— 由交易所持仓模式推导，显式配置优先。

        为什么不能只做手工开关：闸门必须与持仓模式一致，**两侧都会出事** ——
          · single + `no_flip: false` → 单向簿里反向单**不是开反向仓而是净平现有仓**，
            AI 一句 `open_short` 就把整个多头仓平掉；
          · dual + `no_flip: true` → 反向意图本可安全地开成**独立腿**却被拦死；
            更糟的是账户上真出现一条反向腿（误挂 / 手动 / 历史遗留）时，
            **所有 `open_long` 都会被拦** → 网格停摆（实盘 2026-10-16：停摆 4 小时）。

        推导规则：single → True（拦）；dual / split / dual_plus → False（不拦，
        反向 = 独立腿，不会污染本方向）。
        显式写 `no_flip` 就**尊重配置**，但与模式不符会打 WARNING
        ——「配了但不生效 / 生效了但不是想要的」是本仓最贵的失败形态，必须能看出来。
        持仓模式取不到 → fail-safe 取 True（保守一侧，与历史默认一致）。
        """
        explicit = (self.account_risk or {}).get("no_flip", None)
        probe = getattr(self.client, "is_dual_position_mode", None)
        dual: Optional[bool] = None
        if callable(probe):
            try:
                dual = bool(probe())
            except Exception as e:  # noqa: BLE001 — 探测失败按「模式未知」处理
                swallow("executor._effective_no_flip", e, level=logging.DEBUG)
        if explicit is None:
            return True if dual is None else (not dual)
        explicit_b = bool(explicit)
        if dual is not None and explicit_b == dual:
            log.warning(
                "no_flip=%s 与持仓模式(%s)不符 → 闸门语义会错位（单向反向单=净平仓，"
                "双向反向单=独立腿）；建议删掉该键交给模式推导",
                explicit_b, "dual" if dual else "single")
        return explicit_b

    #: 「与策略方向相反的持仓」处置策略白名单
    _STRAY_POLICIES = ("warn", "flatten")

    def _stray_policy(self) -> str:
        """`account_risk.stray_position` 的生效值（非法值回落 `warn`）。"""
        v = str((self.account_risk or {}).get("stray_position", "warn")
                or "warn").strip().lower()
        return v if v in self._STRAY_POLICIES else "warn"

    def flatten_stray(self, symbol: str, pos_short: int, *,
                      owned: Optional[bool] = None) -> dict:
        """处理「与策略方向相反」的持仓 —— 本策略只做多，反向 = 空。

        ⛔ **本能力属于「程序层减仓」，默认关闭（`warn`）且不建议开启。**

        理由（用户 2026-10-16 拍板）：**程序没有识别仓位意图的能力** —— 它只观察到
        「方向 ≠ 策略方向」，而这**不等于**「这条仓是误开的」；`_symbol_owned` 也只证明
        「本 bot 在该合约有挂单」，不证明这条仓的意图。反例：
          · 手动留一条对冲空腿 → 会被程序在 ≤60s 内静默平掉；
          · 将来若做双向网格（如官方「上卖下买」）→ 空腿是**正常仓位**，本方法会与策略打架；
          · 平仓是市价 IOC，**错了不可撤销**。
        分层原则：程序只做「客观可判定」的事（口径 / 硬闸 / 归属 fail-closed / 告警留痕）；
        「这条仓该不该留」是**决策**，归 AI 或人 —— 正确路径是 AI 发 `close_short` /
        `reduce_short`（AI 每轮快照里本来就有 `positions`；这两个动作不受 `no_flip` 拦）。
        本方法仅作为**显式的运维逃生口**保留（`account_risk.stray_position: flatten`
        才启用）—— **不要把它当成默认安全网**。

        两种模式**同一套判据、不同落点**（`close_position` 自己分支）：
          · single：反向 = **净空仓** → `side=None` → `size=0 + close=true` 整簿平；
          · dual  ：反向 = **独立空腿** → 平 `side="short"` 那条腿。

        归属 **fail-closed**：无本 bot 归属证据就不碰（降级 `warn` + WARNING），
        与 `close_all` 的 `_symbol_owned` 判据同源 —— 别人的仓不能平。
        `owned` 可由调用方注入（`ensure_grid_tp` 已经读过档位表，档位表存在本身
        就是归属证据；否则 `-gtp` 卖单被硬闸撤掉后归属会「消失」）。

        注意：平/减仓**不在** `_check_no_flip` 的管辖内（它只拦 `open_*`/`add_*`），
        所以本方法不需要动反手闸门。
        """
        out = {"policy": self._stray_policy(), "position_short": int(pos_short),
               "action": "none"}
        if pos_short <= 0:
            return out
        if out["policy"] != "flatten":
            out["action"] = "warn"
            return out
        if not self.manage_positions:
            out["action"] = "skip_manage_off"
            return out
        if owned is None:
            owned = self._symbol_owned(symbol)
        if not owned:
            out["action"] = "skip_unowned"
            log.warning("stray flatten %s: 有 %d 张空仓但**无本 bot 归属证据** → "
                        "不碰（fail-closed，请人工确认）", symbol, pos_short)
            return out
        dual = False
        probe = getattr(self.client, "is_dual_position_mode", None)
        if callable(probe):
            try:
                dual = bool(probe())
            except Exception as e:  # noqa: BLE001 — 探测失败按 single 处理（side 被忽略）
                swallow("executor.flatten_stray.mode", e, level=logging.DEBUG)
        try:
            # dual 必须给 side（否则 close_position 直接拒）；single 走
            # size=0 + close=true 整簿平，side 会被忽略。
            order = self.client.close_position(symbol, side="short" if dual else None)
        except Exception as e:  # noqa: BLE001 — boundary
            out["action"] = "error"
            out["error"] = str(e)[:200]
            log.warning("stray flatten %s 失败: %s", symbol, e)
            return out
        out["action"] = "flattened"
        out["order_id"] = str((order or {}).get("id") or "")
        log.warning("stray flatten %s: 平掉 %d 张**空仓**（与策略方向相反）→ order=%s；"
                    "下一轮以交易所持仓为准复核", symbol, pos_short, out["order_id"])
        return out

    def _check_no_flip(self, intent: Intent) -> None:
        """禁止同轮反手：持多时不能直接 `open_short`（必须先平）。

        参照 nofx 的 `tradeThrottleReason`（「已有仓位禁止反手开」）。
        反手在同一轮里会先开反向仓、再平旧仓（或反之），保证金占用翻倍，
        且两笔的先后顺序不确定 —— 拆成两轮（先平、下一轮再开）更安全。

        闸门**只拦开仓**（`open_*` / `add_*` / `stop_entry_*`）—— 平仓与减仓
        （`close` / `reduce` / `close_short` / `reduce_short` …）**不在管辖内**，
        所以「持有反向仓时的净平单」本来就走得通，不需要关闸门。

        生效值见 `_effective_no_flip()`：按持仓模式推导（single → 拦；dual → 不拦），
        显式写 `account_risk.no_flip` 时以配置为准。
        """
        if not self._effective_no_flip():
            return
        action = str(intent.action or "").lower()
        if action in ("open_long", "add_long", "stop_entry_long"):
            want = "long"
        elif action in ("open_short", "add_short", "stop_entry_short"):
            want = "short"
        else:
            return
        try:
            positions = self._symbol_positions(intent.symbol)
        except Exception:  # noqa: BLE001 — 取不到持仓就不拦，保持原行为
            return
        for p in positions or []:
            try:
                psz = float(p.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if psz == 0:
                continue
            cur = "long" if psz > 0 else "short"
            if cur != want:
                raise GateApiError(
                    f"NO_FLIP: {getattr(intent, 'symbol', '') or ''} 持有 {cur} 时不能直接开 {want}"
                    f"（先 close，下一轮再开；或配 account_risk.no_flip: false 关闭此闸门）"
                    if getattr(intent, "symbol", "")
                    else f"NO_FLIP: 持有 {cur} 时不能直接开 {want}"
                         f"（先 close，下一轮再开；或配 account_risk.no_flip: false 关闭此闸门）"
                )

    def _check_safe_mode(self, action: str, symbol: str = "") -> None:
        """安全模式：连续失败达阈值时**禁止开仓**（只允许平/减/改/观望）。

        参照 nofx 的 `consecutiveAIFailures>=3` → 过滤掉所有 `open_*`、
        保留 close/hold，且 AI 恢复后**自动退出**（`record_success` 会把
        streak 清零，所以这里不需要额外的解除逻辑）。

        动因：LLM 异常（超时/解析失败/工具循环超限）时，AI 仍可能输出看似
        合理的开仓计划 —— 实测 eth-disc 在薄时段连续 17 轮挂同一个价位。
        安全模式让「AI 不健康时只减不增」。

        阈值配 `account_risk.safe_mode_after_failures`，**0 = 关闭（默认）**，
        所以不配置的 bot 行为完全不变。
        """
        n = int((self.account_risk or {}).get("safe_mode_after_failures") or 0)
        if n <= 0:
            return
        try:
            from .monitoring.health import HealthMonitor
            streak = HealthMonitor(self.root, self.bot_id).current_fail_streak()
        except Exception:  # noqa: BLE001 — 读不到健康数据就不拦，保持原行为
            return
        if streak >= n:
            where = f"{symbol} " if symbol else ""
            raise GateApiError(
                f"SAFE_MODE: {where}连续失败 {streak} 次 ≥ {n}，暂停开仓（只允许平/减/改/观望）"
            )

    def _record_ladder_level(self, intent: Intent, contracts: int,
                             order_id: Any = None) -> None:
        """阶梯档位表落盘（P1）：入场单确认后记 (symbol, price, size, tp_target, label)。

        LLM 的持仓快照只有汇总（size/entry_price），**无分笔成交价** —— 逐档
        止盈对账（每档自己的中值/结构位 TP）因此没有输入。这张表是「每档买入价 /
        张数 / 目标止盈」的本地真相源： strategist 的 account 工具把它注入快照，
        LLM 用「档位表 − open_orders 差集」即得成交档与张数。
        只记 limit 入场腿（open_long/add_long 的 _open 路径）；失败不影响主流程。
        """
        if not self.bot_id:
            return
        try:
            meta = intent.meta or {}
            tp_target = meta.get("tp_target")
            path = (Path(self.root) / "data" / "bots" / self.bot_id
                    / "state" / "ladder_levels.json")
            data: dict[str, Any] = {"levels": []}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 — 损坏则重建，不拦主流程
                    data = {"levels": []}
            levels = [lv for lv in (data.get("levels") or []) if isinstance(lv, dict)]
            levels.append({
                "symbol": intent.symbol,
                "price": float(intent.price or 0),
                "size": int(contracts),
                "tp_target": float(tp_target) if tp_target else None,
                "label": str(intent.label or ""),
                "order_id": str(order_id or ""),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "pending",
            })
            data["levels"] = levels[-200:]  # 有界存储，防无限增长
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)  # 原子替换，避免半写文件
        except Exception as e:  # noqa: BLE001 — 档位表是增强不是关键路径
            swallow("executor._record_ladder_level", e, level=logging.WARNING)

    def _open(self, intent: Intent) -> StepResult:
        self._check_symbol(intent.symbol)
        self._check_safe_mode(intent.action, getattr(intent, "symbol", ""))
        self._check_no_flip(intent)
        self._check_open_sl(intent)
        self._check_account_risk(intent)
        meta = self.client.get_contract(intent.symbol)
        entry_for_risk = intent.price if intent.price is not None else self.client.get_last_price(intent.symbol)
        size_note = ""
        if intent.size_usd is not None:
            intent.size_usd, size_note = self._align_size_to_risk(intent, float(entry_for_risk))
            vol_size, vol_note = self._vol_adjust(intent, intent.size_usd)
            intent.size_usd = vol_size
            if vol_note:
                size_note = f"{size_note}; {vol_note}" if size_note else vol_note
        self._check_notional(intent.size_usd, getattr(intent, "symbol", ""))
        # 挂入场单前先验触发价合法性：保护单挂不上时入场单已挂出 = 无保护挂单
        side_err = self._precheck_exit_triggers(intent)
        if side_err:
            return StepResult(intent.action, intent.symbol, False,
                              detail={"precheck": side_err}, error=side_err)

        # 保证金模式：**AI 显式给 > bot 配置**（`account_risk.margin_mode`）。
        # 幂等 + 切不动就等空窗（Gate 要求无持仓无挂单），不阻塞下单。
        _mm = intent.margin_mode or (self.account_risk or {}).get("margin_mode")
        _mm_res = self._ensure_margin_mode(intent.symbol, _mm) if _mm else ""
        # ⚠️ 全仓（cross）下 `position.leverage` **恒为 0**，杠杆由 `cross_leverage_limit`
        # 决定（实测切换后 `leverage:"0"` / `cross_leverage_limit:"50"`）。此时再调
        # `set_leverage(50)` 会被 Gate 当成「修改杠杆」→ **有挂单时报 ORDER_PENDING**
        # （2026-10-16 18:49 实测：阶梯第 2 档起必然失败 → 整轮 rollback，网格挂不上）。
        # 人格写 `leverage=50` 是**逐仓语义**；全仓下无需设置（`cross_leverage_limit`
        # 已是 50）。逐仓 / 未配 → 照旧设置（行为不变）。
        if intent.leverage and str(_mm or "").strip().lower() != "cross":
            self.client.set_leverage(intent.symbol, int(intent.leverage))

        # 尺寸解析唯一入口（四口径 + 冲突拒单 + 步长量化）。此前这里是
        # `int(intent.size)` —— 0.7 张被截成 0，再被「抬到 1 张」兜底放大 41%。
        try:
            contracts, size_notional, size_basis = self._resolve_order_size(intent, meta)
        except GateApiError as e:
            return StepResult(intent.action, intent.symbol, False,
                              detail={"size_error": str(e)}, error=str(e))
        detail_size = {"size_basis": size_basis, "size_notional": round(size_notional, 4)}

        # 兜底闸门（2026-10-28 实测事故）：19:02 的计划被**重放执行**时，
        # 有 7 条 23~24U 的网格腿最终以 `size=0` 挂到了交易所（text=t-mtg、
        # status=open、left=0）—— 0 张单不成交也不占保证金，但会污染挂单清单、
        # 让对账把「空档」算错。`_resolve_order_size` 自身不会返回 0，所以这里
        # 是**防御性**闸门：任何上游回归都在下单前被拒，绝不静默放行。
        if not contracts or float(contracts) <= 0:
            return StepResult(
                intent.action, intent.symbol, False,
                detail={"size_error": "SIZE_ZERO_REFUSED", **detail_size},
                error=(f"SIZE_ZERO: {intent.symbol} 解析出 {contracts!r} 张，"
                       f"拒绝挂 0 张单（口径 {size_basis}）"),
            )

        # Gate: positive size = buy/long, negative = sell/short
        order_size = contracts if intent.action == "open_long" else -contracts
        body: dict[str, Any] = {"contract": intent.symbol, "size": order_size}
        self._apply_order_type(body, intent.order_type, intent.price, meta)
        if intent.label or self.label_prefix:
            body["text"] = f"t-{self._bot_tag(intent.label)}"

        entry_rec, entry_err = self._place_entry_leg(lambda: self.client.place_order(body), body)
        detail: dict[str, Any] = {
            "order": (entry_rec or {}).get("order") or {},
            "order_check": (entry_rec or {}).get("check") or {},
            "contracts": contracts,
            "size_usd": intent.size_usd,
            **detail_size,
            "order_type": intent.order_type,
            "price": intent.price,
            # 通知卡片用：明确暴露入场/止损/止盈价位
            "entry_price": intent.price if intent.price is not None else (
                self.client.get_last_price(intent.symbol) if entry_rec else None
            ),
            "sl": intent.sl,
            "tp": intent.tp,
            "tp2": intent.tp2,
            "quanto_multiplier": meta.quanto_multiplier,
            "leg_entry_ok": entry_rec is not None,
        }
        if size_note:
            detail["size_align_note"] = size_note
        # 留痕：switched（本次真的切了）/ pending（有挂单，等空窗）。cached 不记。
        if _mm_res and _mm_res != "cached":
            detail["margin_mode"] = _mm_res
        if entry_rec is None:
            return StepResult(
                intent.action, intent.symbol, False, detail=detail,
                error="entry_not_confirmed: " + str(entry_err),
            )
        # P1：阶梯档位表落盘（只记 **limit** 入场腿 —— 市价腿没有档位价，
        # 记进去就是 price=0 的垃圾档，会让 LLM 的「档位表 − open_orders」对账
        # 多出无意义档位；stop_entry 的条件腿走 _stop_entry，不在此处）
        if (intent.action in ("open_long", "open_short", "add_long", "add_short")
                and intent.price):
            self._record_ladder_level(intent, contracts, (entry_rec or {}).get("order", {}).get("id"))

        # auto TP/SL: hang TOGETHER with entry (same planned size) — 不等成交再挂
        pos_side = "long" if intent.action == "open_long" else "short"
        trigger_side = "short" if pos_side == "long" else "long"
        entry_order = (entry_rec or {}).get("order") or {}
        filled = abs(_num(entry_order.get("size"))) - abs(_num(entry_order.get("left")))
        if entry_order.get("finish_as") == "filled":
            filled = abs(_num(entry_order.get("size")))
        # simultaneous hang: exit legs use the SAME size as entry plan
        exit_size = round(float(contracts), 8)
        # grid shared TP/SL: one exit leg sized to total planned contracts
        if intent.tp_size_override and intent.tp is not None:
            exit_size = round(float(intent.tp_size_override), 8)
        detail["exit_size"] = exit_size
        detail["filled_size"] = filled
        detail["hang_mode"] = "simultaneous"
        tp_orders = []
        sl_orders = []
        exit_errors = []
        # 多级止盈：tp/tp2/tp3（缺省均分或 tp1_share/tp2_share）
        # ── 网格腿「去占位 TP」（2026-10-15）─────────────────────────────────
        # 官方做法（Gate/Zoomex/Hummingbot 三源一致）：入场腿是**纯限价买单、不挂
        # TP**，成交后立即在相邻上一格挂反向单。而「占位 TP」正是假网格的根源 ——
        #   ① 必须事后改（实测 LLM 从没改过 → 整仓只剩 1 个共用 TP）；
        #   ② 改的时候撤不掉旧占位（档位表 `executor.py:_record_ladder_level` 在
        #      TP 挂单**之前**跑，没记下 TP 单 id）→ 双 TP / 张数超配；
        #   ③ 占位价 `现价×1.03` 与真实目标无关 → 一旦漏改就是远端假止盈。
        # 故：`grid_tp_auto` 真会挂单（live）时，**网格腿不再挂占位 TP**，逐档 TP
        # 完全交给 `ensure_grid_tp`（成交后按 AI 的 `meta.tp_target` 挂）。
        # **马丁腿（无 `meta.tp_target`）照挂共用 TP** —— 设计需求，不动。
        # 只在 live 时跳过：dry/off 下若也跳过，档位就彻底没有止盈了。
        skip_placeholder_tp = _skip_placeholder_tp(self.account_risk, intent.meta or {})
        legs = [] if skip_placeholder_tp else self._tp_legs(intent, exit_size, meta)
        if skip_placeholder_tp:
            detail["tp_placeholder_skipped"] = "grid_tp_auto=live（改由 ensure_grid_tp 成交后挂）"
        if legs:
            for tp_px, tp_sz, idx in [(p, s, i) for i, (p, s) in enumerate(legs)]:
                rec_, err_ = self._place_exit_leg(
                    lambda p=tp_px, s=tp_sz: (
                        self._place_limit_exit(intent, pos_side, p, is_tp=True, meta=meta, size=s)
                        if intent.tp_mode == "limit_order"
                        else self._place_trigger(intent, trigger_side, p, is_tp=True, meta=meta, size=s)
                    ),
                    kind=f"tp{idx+1}" if len(legs) > 1 else "tp",
                    price_order=(intent.tp_mode != "limit_order"),
                )
                (tp_orders if rec_ else exit_errors).append(rec_ or err_)
        if intent.sl is not None:
            # P1（2026-10-13）：同向同价 SL 归并 —— 详见 `_plan_shared_sl`。
            # 阶梯是逐档调 `_open` 建仓的，每档都带自己的 `intent.sl`；照搬
            # 「每档挂一条 size=本档」就会累积成 N 条同价小单（实测 4 条
            # t-mtg-sl，sz 9/1/1/6）。这里把同价旧单并进来，新单张数 = 旧单之和 +
            # 本档。**先挂新、后撤旧**：新单落盘前旧单一直在（无保护空窗）；新单挂
            # 失败则旧单原样保留（保护不退化）。
            sl_size, merge_ids = self._resolve_sl_size(intent, pos_side, exit_size, meta)
            rec_, err_ = self._place_exit_leg(
                lambda: self._place_limit_exit(intent, pos_side, intent.sl, is_tp=False, meta=meta, size=sl_size)
                if intent.sl_mode == "limit_order"
                else self._place_trigger(intent, trigger_side, intent.sl, is_tp=False, meta=meta, size=sl_size),
                kind="sl", price_order=(intent.sl_mode != "limit_order"),
            )
            if rec_ and merge_ids:
                for _pid in merge_ids:
                    try:
                        self.client.cancel_price_order(_pid)
                    except Exception:  # noqa: BLE001 — 撤旧失败不影响保护（新单已在）
                        log.warning("shared-SL merge: cancel old %s failed", _pid)
            (sl_orders if rec_ else exit_errors).append(rec_ or err_)
        detail["tp_orders"] = tp_orders
        detail["sl_orders"] = sl_orders
        detail["exit_errors"] = exit_errors
        detail["leg_tp_ok"] = skip_placeholder_tp or intent.tp is None or bool(tp_orders)
        detail["leg_sl_ok"] = intent.sl is None or bool(sl_orders)
        detail["exits_ok"] = detail["leg_tp_ok"] and detail["leg_sl_ok"] and not exit_errors
        detail["all_legs_ok"] = detail["leg_entry_ok"] and detail["exits_ok"]
        if exit_errors or not detail["exits_ok"]:
            # entry landed but exits missing — do NOT pretend full success
            detail["rollback"] = self._rollback_unprotected_entry(intent, entry_order)
            return StepResult(
                intent.action,
                intent.symbol,
                False,
                detail=detail,
                error="exit_not_placed: " + "; ".join(str(x) for x in exit_errors or ["tp/sl missing"]),
            )
        # 开仓成功 → 把计划给的浮盈回撤阈值记进状态，供扫描循环按腿取用
        # （扫描是逐 bot 的，不知道当前持仓是哪一轮、按什么阈值开的）
        self.remember_give_back(intent.symbol, intent.side or "long",
                                intent.give_back_pct)
        return StepResult(intent.action, intent.symbol, True, detail=detail)

    def _rollback_unprotected_entry(self, intent: Intent, entry_order: dict,
                                    is_price_order: bool = False) -> str:
        """exits 挂失败后的兜底：不留无保护敞口。

        - 入场单**未成交** → 撤掉它，回到「什么都没挂」的干净状态（无副作用）
        - 入场单**已成交** → **只告警，不自动平仓**

        `is_price_order=True` 用于 `stop_entry_*` —— 突破进场挂在 `/price_orders`，
        撤单要用 `cancel_price_order`。该路径原先**完全没有兜底**：挂保护单失败
        只返回错误、连告警都没有；若此刻条件单已被触发成交，就留下一个
        **无人知晓的裸仓**。

        为什么已成交不自动平仓：这条路径新写、未在实盘验证过。若因误判而自动
        市价平仓，等于**主动扔掉策略本来想持有的仓位** —— 比原问题更主动、更难
        回滚；而且任何一次挂单失败（网络抖动、5xx、保证金不足…）都会触发平仓，
        爆炸半径过大。交给下一轮 plan（≤ interval）+ 人工处理，同时落盘告警。

        注：预检 `_precheck_exit_triggers` 已挡掉已知成因（触发价落在 mark 非法
        一侧），这里是兜底 —— 比如预检与挂单之间 mark 移动了、或交易所因别的
        原因拒绝。
        """
        oid = str(entry_order.get("id") or "")
        # 小数张：Gate 返回字符串 `"0.7"` → `int()` 抛 ValueError，而这里是
        # **「入场单已挂、保护单挂不上」的紧急回滚路径** —— 回滚自己崩掉 = 裸仓挂单留着。
        size = abs(_num(entry_order.get("size")))
        left = abs(_num(entry_order.get("left")))
        filled = size if entry_order.get("finish_as") == "filled" else size - left
        if not oid:
            return "skip: 入场单无 id"
        if filled <= 0:
            try:
                if is_price_order:
                    self.client.cancel_price_order(oid)
                else:
                    self.client.cancel_order(oid)
                return f"cancelled_entry:{oid}"
            except Exception as e:  # noqa: BLE001
                return f"cancel_failed:{oid}:{str(e)[:90]}"
        if self.alert_store is not None:
            try:
                self.alert_store.raise_alert(
                    "unprotected_entry",
                    f"{intent.symbol} {intent.action} 入场已成交 {filled}/{size}，保护单未挂上，需人工/下一轮 plan 处理",
                    symbol=intent.symbol, action=intent.action,
                    entry_order_id=oid, filled=filled, size=size,
                )
            except Exception:  # noqa: BLE001
                pass
        return f"alert_only: 已成交 {filled}/{size} 但保护单未挂上（未自动平仓）"

    def _tp_legs(self, intent: Intent, exit_size: float,
                 meta=None) -> list[tuple[float, float]]:
        """多级止盈腿：任意档位 → `[(price, size)]`，尺寸合计 = `exit_size`。

        **按交易所步长拆**（2026-10-26）：原先全程整数张，ETH 持仓 0.7 张时
        `int(exit_size)` → 0 张（挂出一条 0 张 TP），或按 `round(share)` 补到 1 张
        （reduce-only 超平被拒）。`meta` 缺省按步长 1 处理 = 旧行为逐字一致。

        档位来自 `schema.tp_levels_of()`（唯一来源），**不在这里重写一遍**：
        这条路径原先只认 `tp/tp2/tp3` 三档，档位清单散落 5 处，多一档就少挂一腿。

        份额规则：给了 `tpN_share` 的档按份额，没给的均分剩余；份额之和不为 1
        一律归一化（不做「最后一档兜底吃掉全部」这种静默兜底）。
        """
        pairs = tp_levels_of(intent)
        exit_size = round(float(exit_size or 0), 8)
        if not pairs or exit_size <= 0:
            return []
        step = safe_step(meta) if meta is not None else 1.0
        # 全部按「一个步长」的整数单位运算，最后乘回 step
        total_u = int(round(exit_size / step))
        n = len(pairs)
        if n == 1 or total_u < n:
            # 拆不出「每腿 ≥1 张」时退回单腿（第一档全平）：宁可少一档，
            # 也不要挂出合计超过持仓量的 reduce-only 单。
            return [(pairs[0][0], exit_size)]
        given = {i: min(1.0, max(0.0, float(s)))
                 for i, (_, s) in enumerate(pairs) if s is not None}
        if not given:
            share = [1.0 / n] * n
        else:
            total = sum(given.values())
            free = [i for i in range(n) if i not in given]
            if total > 1.0:
                share = [(given.get(i, 0.0) / total) for i in range(n)]
            else:
                each = ((1.0 - total) / len(free)) if free else 0.0
                share = [given.get(i, each) for i in range(n)]
        legs: list[tuple[float, float]] = []
        remaining = total_u
        for i, (px, _) in enumerate(pairs):
            left = n - i
            if left == 1:
                u = remaining
            else:
                u = int(round(total_u * share[i]))
                # 给后面的腿各留 ≥1 个单位，避免前腿四舍五入把总额撑爆
                u = max(1, min(u, remaining - (left - 1)))
            remaining -= u
            legs.append((float(px), round(u * step, 8)))
        return legs

    def _modify_tp_sl(self, intent: Intent) -> StepResult:
        """Move TP/SL on an existing position (place new first, then drop old owned).

        Only replaces the kind(s) provided: tp only → keep SL; sl only → keep TP.
        """
        self._check_symbol(intent.symbol)
        if intent.tp is None and intent.sl is None:
            return StepResult(
                "modify_tp_sl", intent.symbol, False,
                error="modify_tp_sl requires tp and/or sl",
            )
        positions = self._symbol_positions(intent.symbol)
        if not positions:
            # 良性 no-op：无持仓就没有保护单可调，**不能算失败** —— 否则整轮被记成
            # failed、白烧一个周期并污染失败归档。实测两种成因都会走到这里：
            #   ① 计划同轮里先 reduce/close 再 modify（模拟盘 103 笔）
            #   ② AI 看到「随未成交入场单预挂的保护单」以为有持仓（实盘 07:59 那轮）
            # 与 gate_client.close_position() 对「已平的账本」按 no-op success 处理一致。
            return StepResult(
                "modify_tp_sl", intent.symbol, True,
                detail={"noop": f"no position on {intent.symbol}; nothing to modify"},
            )
        side = intent.side
        if side in ("long", "short"):
            pos = next((p for p in positions if p["side"] == side), None)
            if not pos:
                return StepResult(
                    "modify_tp_sl", intent.symbol, False,
                    error=f"NO_POSITION: no {side} on {intent.symbol}",
                )
        elif len(positions) == 1:
            pos = positions[0]
            side = pos["side"]
        else:
            return StepResult(
                "modify_tp_sl", intent.symbol, False,
                error="AMBIGUOUS_SIDE: dual position requires side=long|short",
            )
        # SL 移动纪律（用户 2026-10-12 晚复盘修正）：判据是「持仓是否浮盈」
        # （mark vs entry），不是新 SL 相对 entry。18:01 实例：浮盈 +7.7 点时
        # SL 2539.3→2545.9（< entry 2551.2）是合法锁盈，首版判据会误拒。
        # prefix/owned 提前取（纪律校验要读旧 SL），后面复用不再重复请求。
        prefix = self._own_prefix(intent.label)
        owned = self._owned_price_orders(intent.symbol, prefix) if prefix else []

        def _kind_of(po: dict) -> str:
            text = str((po.get("initial") or {}).get("text") or po.get("text") or "")
            if text.endswith("-tp") or text.endswith("-lp"):
                return "tp"
            if text.endswith("-sl") or text.endswith("-ls"):
                return "sl"
            return ""

        entry = float(pos.get("entry_price") or 0)
        mark = float(pos.get("mark_price") or 0)
        sl_rejected_reason = ""
        if intent.sl is not None and entry > 0:
            sl_prices = []
            for po in owned:
                if _kind_of(po) != "sl":
                    continue
                trig = po.get("trigger") or {}
                px = _to_float(trig.get("price")
                               or (po.get("initial") or {}).get("trigger_price")
                               or 0)
                if px > 0:
                    sl_prices.append(px)
            if sl_prices:
                # 紧的那条 = 多头里最高、空头里最低（多条 SL 属历史遗留）
                old_sl = (max(sl_prices) if side == "long" else min(sl_prices))
            else:
                old_sl = 0.0
            # 策略分派：阶梯/马丁网格走 "ladder"（允许把推浅的 SL 挪回深位），
            # 单腿 brooks 类保持 "strict"。配置项 account_risk.sl_move_policy。
            sl_policy = str((self.account_risk or {}).get("sl_move_policy")
                            or "strict")
            reason = _sl_move_rejected(side, entry, float(intent.sl),
                                       old_sl=old_sl, mark=mark,
                                       policy=sl_policy)
            if reason:
                log.warning("SL_REJECT(modify) %s %s: %s (保留旧 SL)",
                            intent.symbol, side, reason)
                sl_rejected_reason = reason
                if intent.tp is None:
                    # 纯 SL 修改被拒：旧 SL 原样保留，本轮良性 no-op（不算失败）
                    return StepResult(
                        "modify_tp_sl", intent.symbol, True,
                        detail={"sl_rejected": reason,
                                "noop": "sl move blocked; old sl kept"},
                    )
                # TP+SL 同改：丢 SL 腿、TP 照改（撤旧逻辑见 intent.sl is None 跳过旧 SL）
                log.info("SL_REJECT(modify) %s: SL 腿丢弃，TP 照改", intent.symbol)
                intent.sl = None
        size = abs(_num(pos["size"]))
        meta = self.client.get_contract(intent.symbol)
        pos_side = side
        trigger_side = "short" if pos_side == "long" else "long"
        open_action = "open_long" if pos_side == "long" else "open_short"
        # rule direction depends on position side
        if intent.trigger_rule_tp is None:
            intent.trigger_rule_tp = infer_trigger_rules(open_action, True)
        if intent.trigger_rule_sl is None:
            intent.trigger_rule_sl = infer_trigger_rules(open_action, False)
        # _place_trigger signs by intent.side
        intent.side = pos_side

        new_ids: set[str] = set()
        detail: dict[str, Any] = {
            "position_side": pos_side,
            "position_size": size,
            "prefix": prefix,
        }
        if sl_rejected_reason:
            detail["sl_rejected"] = sl_rejected_reason
        errors: list[str] = []

        # 触发价合法性（E1 分治）：modify 路径**非法腿跳过 + 留痕**，合法腿照挂。
        # 入口路径（_open/_stop_entry）仍是「一腿非法整笔中止」—— 未成交挂单可以
        # 宁可不交易；但 modify 面对的是**已成交持仓**，一腿非法全拒的后果是
        # 「一档中值够不着 → 所有档位永远改不了」（2026-10-15 实盘：网格档中值
        # 在现价下方，LLM 连续 10+ 轮只能 hold，共用 TP 假网格的成因之一）。
        skipped_illegal: list[str] = []
        if intent.tp is not None and intent.tp_mode != "limit_order":
            rule_tp = intent.trigger_rule_tp
            if rule_tp is None:
                rule_tp = infer_trigger_rules(open_action, True)
            pairs = tp_levels_of(intent)
            good, bad = self._filter_illegal_exit_legs(
                intent, [("tp", float(px), int(rule_tp)) for px, _ in pairs])
            if bad:
                skipped_illegal.extend(bad)
                log.warning(
                    "TP_SKIP_ILLEGAL(modify) %s %s: %s（该档跳过，其余档照挂）",
                    intent.symbol, side, "; ".join(bad))
            legal_px = {px for _, px, _ in good}
            kept = [(px, sh) for px, sh in pairs if float(px) in legal_px]
            if kept:
                # 只保留合法档：整体重建档位字段（只改 tp 会留下 tp2/tp3 串档）
                self._rebuild_tp_levels(intent, kept)
            else:
                intent.tp = None  # 全部非法 → 等同未提供 tp
        if intent.sl is not None and intent.sl_mode != "limit_order":
            rule_sl = intent.trigger_rule_sl
            if rule_sl is None:
                rule_sl = infer_trigger_rules(open_action, False)
            good, bad = self._filter_illegal_exit_legs(
                intent, [("sl", float(intent.sl), int(rule_sl))])
            if not good:
                skipped_illegal.append(f"sl={float(intent.sl):g} 非法")
                log.warning(
                    "SL_SKIP_ILLEGAL(modify) %s %s: %s（SL 腿跳过，TP 照改）",
                    intent.symbol, side, "; ".join(bad))
                intent.sl = None
        if skipped_illegal:
            detail["skipped_illegal"] = skipped_illegal
        if intent.tp is None and intent.sl is None:
            # 全部退出腿非法：良性 no-op（旧结构原样保留），不算失败
            return StepResult(
                "modify_tp_sl", intent.symbol, True,
                detail={"skipped_illegal": skipped_illegal,
                        "noop": "all exit legs illegal; old structure kept"},
            )

        # TP 可能是**多腿**（任意档位）—— 与 `_open` 同源用 `_tp_legs` 展开。
        # 原先这里只挂 `intent.tp` 一条，却在下面对**所有**旧 TP 执行撤单，
        # 于是双止盈结构在第一次 modify 之后就塌成单腿（TP2 永久消失）。
        # 实测：brooks-btc 372 次改单、**0 次**挂出 ≥2 条 TP（而开仓 169 次里 140 次双止盈）。
        tp_legs = self._tp_legs(intent, size, meta) if intent.tp is not None else []
        planned: list[tuple[str, float, float, str]] = [
            (f"tp{i + 1}" if len(tp_legs) > 1 else "tp", px, round(sz, 8), intent.tp_mode)
            for i, (px, sz) in enumerate(tp_legs)
        ]
        if intent.sl is not None:
            planned.append(("sl", float(intent.sl), round(float(size), 8), intent.sl_mode))

        # `tp_orders` / `sl_orders` 与 `_open` / `_stop_entry` **同字段同形状** ——
        # 通知卡片（`notify._exit_levels`）、`exchange_pnl` 归属、上线前脚本都按这两个
        # 字段取「实际挂出的腿」。此前 modify 只写单腿的 `tp_placed`，多腿结构在
        # 卡片上只显示第一档、TP2 的成交也归不了属。
        tp_orders: list[dict] = []
        sl_orders: list[dict] = []
        for kind, price, leg_size, mode in planned:
            is_tp = kind.startswith("tp")
            try:
                rec_, err_ = self._place_exit_leg(
                    lambda p=price, m=mode, sz=leg_size, t=is_tp: (
                        self._place_limit_exit(intent, pos_side, p, is_tp=t, meta=meta, size=sz)
                        if m == "limit_order"
                        else self._place_trigger(
                            intent, trigger_side, p, is_tp=t, meta=meta, size=sz
                        )
                    ),
                    kind=kind,
                    price_order=(mode != "limit_order"),
                )
            except Exception as e:  # noqa: BLE001 — boundary
                rec_, err_ = None, str(e)
            if rec_:
                order = rec_.get("order") or {}
                oid = str(order.get("id") or "")
                if oid:
                    new_ids.add(oid)
                entry = {"id": oid, "price": price, "size": leg_size,
                         "check": rec_.get("check")}
                if is_tp:
                    tp_orders.append(rec_)
                else:
                    sl_orders.append(rec_)
                    detail["sl_placed"] = entry
                # 统一字段名：与 open_* 的 detail.tp/detail.sl 对齐，供卡片/日志等消费者直读。
                # 单腿时才写 `tp_placed` —— 多腿用 `tp_orders`（与 `_open` 同源）。
                if is_tp and len(tp_legs) == 1:
                    detail["tp_placed"] = entry
            else:
                errors.append(f"{kind}: {err_}")
                detail[f"{kind}_error"] = str(err_)
        if tp_legs:
            detail["tp"] = tp_legs[0][0]      # 兼容：多数消费者只看第一档
            detail["tp_legs"] = [px for px, _ in tp_legs]
        if tp_orders:
            detail["tp_orders"] = tp_orders
        if sl_orders:
            detail["sl_orders"] = sl_orders
            detail["sl"] = float(intent.sl)

        # cancel old owned TP/SL of the kinds we just replaced (never stop_entry)
        cancelled = []
        for po in owned:
            kind = _kind_of(po)
            if not kind or kind not in ("tp", "sl"):
                continue
            # only replace kinds the caller asked to move
            if (kind == "tp" and intent.tp is None) or (kind == "sl" and intent.sl is None):
                continue
            status = str(po.get("status") or "").lower()
            if status in _TERMINAL_PO_STATUS:
                continue  # already terminal — do not re-cancel
            pid = self._order_id(po)
            if not pid or pid in new_ids:
                continue
            try:
                self.client.cancel_price_order(pid)
                cancelled.append(pid)
            except Exception as e:  # noqa: BLE001
                errors.append(f"cancel {pid}: {e}")
        detail["cancelled_old"] = cancelled

        # E2：TP 结构塌方告警 —— 新 TP 腿数 < 旧 TP 腿数（多腿结构被单腿替换）
        # 必须可见：2026-10-15 假网格事故的引擎侧形态就是「每档独立 TP 的结构 →
        # 1 个无 share 单 tp 覆盖全仓」。留痕不拦（人格层有硬禁令兜底）。
        old_tp_live = sum(
            1 for po in owned
            if _kind_of(po) == "tp"
            and str(po.get("status") or "").lower() not in _TERMINAL_PO_STATUS
        )
        if len(tp_legs) < old_tp_live:
            log.warning(
                "TP_STRUCT_COLLAPSE %s %s: 旧 %d 腿 TP → 新 %d 腿（多腿结构被单腿替换）",
                intent.symbol, side, old_tp_live, len(tp_legs))
            detail["tp_struct_collapse"] = {
                "old_legs": old_tp_live, "new_legs": len(tp_legs)}

        ok = not errors
        return StepResult(
            "modify_tp_sl",
            intent.symbol,
            ok,
            detail=detail,
            error="; ".join(errors) if errors else "",
        )

    def _find_open_order_by_text(self, symbol: str, text: str) -> Optional[dict]:
        if not text:
            return None
        try:
            rows = self.client.list_orders(symbol) or []
        except Exception:  # noqa: BLE001
            return None
        for o in rows:
            if str(o.get("text") or "") == text:
                return o
        return None

    def _place_entry_leg(self, placer, body: dict) -> tuple[Optional[dict], Optional[str]]:
        """Place entry with confirmation; retry only when order is NOT on exchange."""
        text = str(body.get("text") or "")
        last_err = None
        for attempt in (1, 2):
            if attempt == 2:
                # idempotent: if first place landed but confirm flaked, do NOT double-place
                existing = self._find_open_order_by_text(body.get("contract"), text)
                if existing and existing.get("id"):
                    check = self._confirm_order_landed(existing, kind="entry")
                    return {"order": existing, "check": check}, None
            try:
                order = placer()
            except GateApiError as e:
                last_err = f"entry#{attempt} {e}"
                continue
            check = self._confirm_order_landed(order, kind="entry")
            if check.get("ok") and check.get("confirmed"):
                return {"order": order, "check": check}, None
            last_err = f"entry#{attempt} not_confirmed id={(order or {}).get('id')}"
        return None, last_err

    def _confirm_order_landed(self, order: dict, kind: str = "entry") -> dict:
        """Re-read the order on exchange; reject empty/failed placements."""
        oid = (order or {}).get("id")
        status = str((order or {}).get("status") or "")
        finish_as = str((order or {}).get("finish_as") or "")
        check = {
            "kind": kind,
            "id": oid,
            "status": status,
            "finish_as": finish_as,
            "left": (order or {}).get("left"),
            "ok": bool(oid) and status not in ("cancelled", "failed", "reduced"),
        }
        if not check["ok"]:
            return check
        try:
            live = self.client.get_order(str(oid))
            check["live_status"] = live.get("status")
            check["live_left"] = live.get("left")
            # still visible as open or already finished/canceled-after-fill
            check["confirmed"] = live.get("id") is not None
        except Exception as e:  # noqa: BLE001
            check["confirm_error"] = str(e)
            check["confirmed"] = False
        return check

    def _confirm_price_order_landed(self, order: dict, kind: str = "trigger") -> dict:
        oid = (order or {}).get("id")
        check = {"kind": kind, "id": oid, "ok": bool(oid), "confirmed": False}
        if not oid:
            return check
        try:
            live = self.client.get_price_order(str(oid))
            check["live_status"] = live.get("status")
            check["confirmed"] = live.get("id") is not None and str(live.get("status") or "") != "cancelled"
        except Exception as e:  # noqa: BLE001
            # fall back to open list
            try:
                found = [
                    p for p in (self.client.list_price_orders(order.get("body", {}).get("initial", {}).get("contract"))
                                or [])
                    if str(p.get("id")) == str(oid)
                ]
                check["confirmed"] = bool(found)
            except Exception as e2:  # noqa: BLE001
                check["confirm_error"] = f"{e} / {e2}"
        return check

    def _place_exit_leg(self, placer, kind: str, price_order: bool) -> tuple[Optional[dict], Optional[str]]:
        """Place one exit leg with confirmation; retry only if not on exchange."""
        last_err = None
        for attempt in (1, 2):
            if attempt == 2 and last_err and "not_confirmed" in str(last_err):
                # do not blindly re-place when first attempt may have landed
                break
            try:
                rec = placer()
            except GateApiError as e:
                last_err = f"{kind}#{attempt} {e}"
                continue
            raw = dict(rec or {})
            inner = raw.get("order")
            if isinstance(inner, dict):
                order = dict(inner)
            else:
                # raw is the order payload itself (place_price_order / _place_trigger)
                order = {k: v for k, v in raw.items() if k not in ("order", "check")}
            check = (
                self._confirm_price_order_landed(order, kind=kind)
                if price_order
                else self._confirm_order_landed(order, kind=kind)
            )
            rec = dict(raw)
            rec["order"] = order
            rec["check"] = check
            if check.get("ok", True) and (check.get("confirmed") or order.get("finish_as") == "finished"):
                return rec, None
            last_err = f"{kind}#{attempt} not_confirmed id={order.get('id')}"
        return None, last_err or f"{kind} failed"

    def _place_limit_exit(self, intent: Intent, pos_side: str, price: float, is_tp: bool, meta, size: int) -> dict:
        """Resting reduce_only LIMIT exit (限价止盈/止损挂单), not a price trigger.

        long  → sell limit (negative size)
        short → buy limit (positive size)
        Note: a stop-loss limit that is immediately marketable will fill at once;
        use sl_mode=trigger for true stop semantics.
        """
        api_size = -abs(size) if pos_side == "long" else abs(size)
        px = float(price)

        def _body(p: float) -> dict:
            return {
                "contract": intent.symbol,
                "size": api_size,
                "price": str(round_price(p, meta)),
                "tif": "gtc",
                "reduce_only": True,
                "text": f"t-{self._bot_tag(intent.label)}-{'lp' if is_tp else 'ls'}",
            }

        body = _body(px)
        try:
            order = self.client.place_order(body)
        except GateApiError as e:
            if "PRICE_TOO_DEVIATED" not in str(e) and "MARKET_PRICE_TOO_DEVIATED" not in str(e):
                raise
            # one clamp toward book mid so resting exit still lands
            mid = self._book_mid(intent.symbol)
            if mid is None:
                raise
            side = "down" if pos_side == "long" else "up"
            clamped = self._clamp_toward(px, mid, side=side, max_bps=80)
            body = _body(clamped)
            order = self.client.place_order(body)
            body["price_clamped_from"] = round_price(px, meta)
        return {
            "mode": "limit_order",
            "kind": "tp" if is_tp else "sl",
            "order": order,
            "body": body,
        }

    def _book_mid(self, symbol: str) -> Optional[float]:
        try:
            ob = self.client.get_orderbook_top(symbol, limit=1)
            bids, asks = ob.get("bids") or [], ob.get("asks") or []
            b = float((bids[0] or {}).get("p")) if bids else None
            a = float((asks[0] or {}).get("p")) if asks else None
            if b is not None and a is not None:
                return (b + a) / 2.0
            return b or a
        except Exception:  # noqa: BLE001
            return None

    def _clamp_toward(self, px: float, mid: float, side: str = "down", max_bps: float = 80) -> float:
        limit = mid * (1 - max_bps / 10000.0) if side == "down" else mid * (1 + max_bps / 10000.0)
        return min(px, limit) if side == "down" else max(px, limit)

    def _trail(self, intent: Intent) -> StepResult:
        self._check_symbol(intent.symbol)
        amount = abs(_num(intent.size))
        if intent.side != "long":
            amount = -amount
        body = {
            "contract": intent.symbol,
            # 张数走字符串：整数张必须仍是 "3"（不能变 "3.0"），小数张是 "0.7"
            "amount": _fmt_qty(amount),
            "activation_price": str(intent.activation_price or "0"),
            "price_offset": str(intent.price_offset),
        }
        order = self.client.place_trailing_order(body)
        return StepResult("trail", intent.symbol, True, detail={"order": order, "body": body})

    def _cancel_trail_all(self, symbol: str, scope: str = "") -> StepResult:
        if symbol:
            # 带 symbol 的老路径：逐字不变（单币 bot 走这条）
            self._check_symbol(symbol)
            result = self.client.stop_trailing_orders(symbol)
            return StepResult("cancel_trail_all", symbol, True, detail={"result": result})
        # 空 symbol：必须显式 scope（原先静默 `stop_trailing_orders(None)` = 全账户）
        scope = self._wide_scope(scope, "cancel_trail_all")
        if scope == "account":
            result = self.client.stop_trailing_orders(None)
            return StepResult("cancel_trail_all", "", True,
                              detail={"result": result, "scope": "account"})
        symbols = self._wide_symbols(scope, list(self.symbols_whitelist or []) + self._open_symbols())
        if not symbols:
            raise GateApiError(
                "cancel_trail_all: scope=bot 下没有可撤的 symbol"
                "（本 bot 未声明 symbols 且当前无持仓）")
        result = {sym: self.client.stop_trailing_orders(sym) for sym in symbols}
        return StepResult("cancel_trail_all", "", True,
                          detail={"result": result, "scope": "bot", "symbols": symbols})

    def _stop_entry(self, intent: Intent) -> StepResult:
        """Breakout ENTRY: trigger then OPEN. Not stop-loss."""
        self._check_symbol(intent.symbol)
        self._check_safe_mode(intent.action, getattr(intent, "symbol", ""))
        self._check_no_flip(intent)
        # 与 `_open` 完全同源的两道账户/计划闸门（原先只有 _check_symbol + _check_notional）。
        # 缺 `_check_open_sl` / `_check_account_risk` 时，突破单可以绕过
        # halt / daily_loss_limit / max_leverage / 总敞口闸门 / SL 必填 ——
        # 而 eth-disc 实测 4 轮里 3 轮产出的正是 `stop_entry_long`，
        # 等于账户级风控在多数单子上根本没生效。
        self._check_open_sl(intent)
        self._check_account_risk(intent)
        meta = self.client.get_contract(intent.symbol)
        # 与 `_open` 同源：先按「权益 × risk_pct ÷ 止损距离」反推名义，再做波动率调整。
        # 缺这两步时 stop_entry 的仓位只受 max_notional 限制 —— 风险公式被整个绕过。
        # 实测（eth-disc 讨论组）4 轮里 3 轮产出的正是 stop_entry_long，于是
        # 「每笔风险 X% 本金」在多数单子上不生效，仓位直接顶到 max_notional 上限。
        size_note = ""
        if intent.size is None and intent.size_usd is not None:
            entry_for_risk = (intent.price if intent.price is not None
                              else self.client.get_last_price(intent.symbol))
            if entry_for_risk:
                intent.size_usd, size_note = self._align_size_to_risk(
                    intent, float(entry_for_risk))
                intent.size_usd, vol_note = self._vol_adjust(intent, intent.size_usd)
                if vol_note:
                    size_note = f"{size_note}; {vol_note}" if size_note else vol_note
        self._check_notional(intent.size_usd, getattr(intent, "symbol", ""))
        # 挂入场单前先验 TP/SL 触发价合法性：保护单挂不上时入场单已挂出 = 无保护挂单
        # （与 `_open` 同源。**注意它校验的是保护单的触发价，不含 entry 的
        #  `trigger_price`** —— 后者只有交易所会校验，见 UPGRADE-PLAN 待办）
        side_err = self._precheck_exit_triggers(intent)
        if side_err:
            return StepResult(intent.action, intent.symbol, False,
                              detail={"precheck": side_err}, error=side_err)
        # 与 `_open` **同源**（此前两处各自抄了一份同样的 int 截断）
        try:
            contracts, size_notional, size_basis = self._resolve_order_size(intent, meta)
        except GateApiError as e:
            return StepResult(intent.action, intent.symbol, False,
                              detail={"size_error": str(e)}, error=str(e))
        signed = contracts if intent.action == "stop_entry_long" else -contracts
        if intent.order_type == "market":
            initial = {"contract": intent.symbol, "size": signed, "price": "0", "tif": "ioc"}
        else:
            initial = {
                "contract": intent.symbol,
                "size": signed,
                "price": str(round_price(float(intent.price), meta)),
                "tif": {"limit": "gtc", "post_only": "poc", "ioc": "ioc", "fok": "fok"}[intent.order_type],
            }
        if intent.label or self.label_prefix:
            initial["text"] = f"t-{self._bot_tag(intent.label)}"
        body = {
            "initial": initial,
            "trigger": {
                "strategy_type": 0,
                "price_type": PRICE_TYPE_MAP.get(intent.trigger_price_type, 0),
                "price": str(intent.trigger_price_tp),
                "rule": int(intent.trigger_rule_tp or (1 if intent.action == "stop_entry_long" else 2)),
            },
        }
        if intent.trigger_expiration and self.client.env == "live":
            body["trigger"]["expiration"] = int(intent.trigger_expiration)
        order = self.client.place_price_order(body)
        check = self._confirm_price_order_landed(order, kind="stop_entry")
        ok = bool(check.get("ok") and check.get("confirmed"))
        detail = {"order": order, "body": body, "order_check": check}
        if size_note:
            detail["size_note"] = size_note
        if intent.size_usd is not None:
            detail["size_usd"] = float(intent.size_usd)
        if not ok:
            return StepResult(intent.action, intent.symbol, False, detail=detail,
                              error=f"stop_entry not confirmed id={order.get('id')}")
        # 突破入场也要预挂保护（含双止盈），成交即生效
        if intent.sl is not None or intent.tp is not None:
            contracts_abs = abs(float(contracts))
            pos_side = "long" if intent.action == "stop_entry_long" else "short"
            trigger_side = "short" if pos_side == "long" else "long"
            # stop_entry 用 trigger_price 作入场参考算 SL 规则方向
            saved_side = intent.side
            intent.side = pos_side
            if intent.trigger_rule_sl is None:
                intent.trigger_rule_sl = infer_trigger_rules(
                    "open_long" if pos_side == "long" else "open_short", False
                )
            if intent.trigger_rule_tp is None:
                intent.trigger_rule_tp = infer_trigger_rules(
                    "open_long" if pos_side == "long" else "open_short", True
                )
            tp1_share = float(intent.tp1_share if intent.tp1_share is not None else 0.5)
            tp1_share = min(1.0, max(0.0, tp1_share))
            tps = []
            sls = []
            errs = []
            legs = self._tp_legs(intent, contracts_abs, meta)
            for tp_px, tp_sz, idx in [(p, s, i) for i, (p, s) in enumerate(legs)]:
                rec_, err_ = self._place_exit_leg(
                    lambda p=tp_px, s=tp_sz: self._place_trigger(
                        intent, trigger_side, p, is_tp=True, meta=meta, size=s
                    ),
                    kind=f"tp{idx+1}" if len(legs) > 1 else "tp", price_order=True,
                )
                (tps if rec_ else errs).append(rec_ or err_)
            if intent.sl is not None:
                rec_, err_ = self._place_exit_leg(
                    lambda: self._place_trigger(
                        intent, trigger_side, intent.sl, is_tp=False, meta=meta, size=contracts_abs
                    ),
                    kind="sl", price_order=True,
                )
                (sls if rec_ else errs).append(rec_ or err_)
            intent.side = saved_side
            detail["tp_orders"] = tps
            detail["sl_orders"] = sls
            detail["exit_errors"] = errs
            detail["hang_mode"] = "with_stop_entry"
            if errs:
                # 与 `_open` 同源：保护单挂不上时不留无保护敞口。
                # stop_entry 是条件单 —— 未触发则撤掉它；若已被触发成交，
                # `_rollback_unprotected_entry` 会落盘告警（不自动平仓）。
                detail["rollback"] = self._rollback_unprotected_entry(
                    intent, order, is_price_order=True)
                return StepResult(intent.action, intent.symbol, False, detail=detail,
                                  error="exit_not_placed: " + "; ".join(str(x) for x in errs))
        # 突破单已挂出（尚未成交）→ 先记下阈值。持仓出现时扫描循环就能按腿取到。
        # 用 `intent.side`（`_parse_stop_entry` 必定设置），不要用上面那个只在该
        # 分支里定义的 `pos_side` —— 无 SL/TP 时它不存在。
        self.remember_give_back(intent.symbol, intent.side or "short",
                                intent.give_back_pct)
        return StepResult(intent.action, intent.symbol, True, detail=detail)

    @staticmethod
    def _order_id(po: dict) -> str:
        """Gate 用 `id`，paper 用 `order_id` —— 两个都认。"""
        return str(po.get("id") or po.get("order_id") or po.get("id_string") or "")

    @staticmethod
    def _order_is_reduce_only(po: dict) -> bool:
        """Gate 返回 `initial.is_reduce_only`，paper 侧用 `reduce_only` —— 两个都认。"""
        init = po.get("initial") or {}
        return bool(
            init.get("is_reduce_only")
            or init.get("reduce_only")
            or po.get("is_reduce_only")
            or po.get("reduce_only")
        )

    def _has_pending_entry(self, symbol: str) -> bool:
        """该 symbol 上是否还有本 bot 未成交的入场委托（**普通单 + 条件单**）。

        入场单挂出后 1-3 秒，执行器就把它的 TP/SL 一起挂上了 —— 此时账户还没有持仓。
        若按「无持仓」把这些保护单当孤儿撤掉，委托一成交就是**裸仓**（无 SL/TP）。
        所以只要有待成交入场单，本 symbol 的保护单一律保留；真孤儿留到下一轮扫描再清。

        **必须同时扫条件单**：`stop_entry_*`（突破进场）是**条件单**，挂在
        `/price_orders` 而不是 `/orders`。只扫普通单会漏掉它们 —— 线上实测
        2026-10-02 16:12 / 16:17 / 16:28 连续三次把突破进场单的保护单当孤儿撤掉，
        而当时 mark 距触发只差 0.17%。
        """
        # 1) 普通挂单（limit 未成交的入场单）
        try:
            rows = self._owned_open_orders(symbol, self.label_prefix)
        except Exception:  # noqa: BLE001 — 取不到就当没有，保持原行为
            rows = []
        for o in rows:
            if o.get("is_reduce_only"):
                continue
            # `left` 是**带符号**的剩余量：Gate 对卖单返回负的 size/left
            # （实测 sell 15 张 → size=-15 left=-15）。所以这里只能判 `== 0`
            # （已全部成交），**不能写 `<= 0`** —— 那会把**所有空头入场单**误判成
            # 「没有待成交入场单」，孤儿扫描随即失去豁免、撤掉预挂保护单，
            # 委托一成交就是裸仓。
            # 线上实测 2026-10-02 17:35:03：17:31 `open_short` 挂出 85300 空单
            # （left=-15）+ 3 张保护单，4 分钟后 3 张保护单被孤儿扫描全部撤掉，
            # 而入场单仍在挂。
            # 同文件 :830 / :902 早已用 abs() 处理同一个符号问题，只有这里漏了。
            left = o.get("left")
            if left is not None and int(left) == 0:
                continue
            return True
        # 2) 条件单（stop_entry_* 突破进场，未触发）
        try:
            conds = self._owned_price_orders(symbol, self.label_prefix)
        except Exception:  # noqa: BLE001
            conds = []
        for p in conds:
            if self._order_is_reduce_only(p):
                continue  # reduce_only = 保护单，不是入场单
            status = str(p.get("status") or "").lower()
            if status in ("cancelled", "finished", "filled", "triggered", "failed", "closed"):
                continue  # 已终结
            return True
        return False

    def _is_orphan_protector(self, po: dict, positions: list) -> bool:
        """判断保护单是否孤儿（无对应持仓）。

        保护单方向：sell(-size) 平多单，buy(+size) 平空单。
        有对应持仓 → 当前计划保护单，保留；无 → 孤儿。

        **contract 必须比对**：两个调用方传来的持仓都带 `contract`
        （`_symbol_positions` 的输出由 `_parse_symbol_positions` 统一补上）。
        不比的话「BTC 的孤儿保护单」会被「ETH 的多仓」误判成有对应持仓而**永远清不掉**
        —— 实测 2026-10-04 复核发现，受影响账户如 `brooks-ab-paper` / `ed-paper` /
        `nial-paper` 都是多币种同向持仓。

        仍保留「两边都有 contract 才比」的守卫：持仓记录来自交易所客户端，
        这是系统边界；缺字段时退化为只比方向（宁可多留，不可误撤）。
        """
        init = po.get("initial") or {}
        text = str(init.get("text") or po.get("text") or "")
        tail = text.rsplit("-", 1)[-1].lower() if text else ""
        if tail not in ("tp", "sl", "lp", "ls"):
            return False
        if not self._order_is_reduce_only(po):
            return False
        # 保护单方向：负=卖平多，正=买平空；venue-computed 腿（size=0）靠 auto_size
        want = _protector_side(init) or _protector_side(po)
        if want is None:
            return True  # 无方向无法匹配 → 保守当孤儿
        o_contract = str(init.get("contract") or po.get("contract") or "")
        for p in positions:
            psz = float(p.get("size") or 0)
            if psz == 0:
                continue
            p_contract = str(p.get("contract") or "")
            if p_contract and o_contract and p_contract != o_contract:
                continue  # 别的币的仓位不算「对应持仓」
            side = "long" if psz > 0 else "short"
            if side == want:
                return False  # 有对应持仓 → 不是孤儿
        return True

    def _cleanup_orphan_protectors(self, symbol: str, keep_ids: Optional[set] = None,
                                   label: str = "") -> list:
        """回收孤儿保护单（有对应持仓的保留）。

        安全闸：reduce_only + tp/sl 后缀 + 本 bot 命名空间 + 非 keep + **无对应持仓** + **无待成交入场单**。

        归属用 `label_prefix` 优先、否则用本次信号的 `label`；**两者都空就一张都不撤**
        （fail-closed）—— 原先的形态是「有 prefix 才过滤」，一旦漏配 `label_prefix`
        就静默退化成整表撤单（会撤掉别的 bot 的保护单）。
        """
        if not symbol:
            return []
        tag = self._own_scope_tag(label)
        if not tag:
            log.warning("_cleanup_orphan_protectors(%s): skipped_unattributed —— "
                        "label_prefix 与 label 均为空，判不出本 bot 命名空间，"
                        "拒绝处理任何保护单（原先会退化成整表）", symbol)
            return []
        keep = {str(x) for x in (keep_ids or set())}
        # 有待成交入场单 → 预挂的 TP/SL 不是孤儿，撤了会裸仓（symbol 级判定）
        if self._has_pending_entry(symbol):
            return []
        try:
            positions = self._symbol_positions(symbol)
            rows = self.client.list_price_orders(symbol) or []
        except Exception:  # noqa: BLE001
            return []
        cancelled = []
        for p in rows:
            init = p.get("initial") or {}
            text = str(init.get("text") or p.get("text") or "")
            tail = text.rsplit("-", 1)[-1].lower() if text else ""
            if tail not in ("tp", "sl", "lp", "ls"):
                continue
            if not self._order_is_reduce_only(p):
                continue
            status = str(p.get("status") or "").lower()
            if status in ("cancelled", "finished", "filled", "triggered", "failed", "closed"):
                continue
            pid = self._order_id(p)
            if not pid or pid in keep:
                continue
            if not self._text_owned(text, tag):
                continue
            if not self._is_orphan_protector(p, positions):
                continue  # 当前计划保护单，保留
            try:
                self.client.cancel_price_order(pid)
                cancelled.append(pid)
            except Exception:  # noqa: BLE001
                pass
        # P0.4：发现并回收孤儿保护单 → 落盘告警
        if cancelled and self.alert_store is not None:
            try:
                self.alert_store.orphan(symbol, len(cancelled), cancelled)
            except Exception:  # noqa: BLE001
                pass
        return cancelled

    def _resync_protectors(self, symbol: str, label: str = "") -> list:
        """减仓/调仓后把保护单张数同步到剩余持仓，并**归并同向同价的 SL**。

        - flat：交给 _cleanup_orphan_protectors
        - 有仓：
          * **SL**：按 `(方向, 触发价)` 归并 —— 同一价位只留一条，张数 = **该方向**
            的持仓量。原先「逐条重挂」在阶梯分腿 SL（实测 4 条同价 9/1/1/6）上会
            撤 4 挂 4，每条都放大到全 symbol 的 total → 保护单**超配数倍**；双向
            持仓时还会拿另一条腿的张数去挂本方向（语义就错了）。
          * **TP / limit 腿**：按「组」对齐（`_align_tp_group`）—— 多级止盈
            张数合计 == 持仓 → 逐档结构保持（零 churn）；**存在全仓腿（马丁共用
            TP 覆盖全仓 + 逐档 TP 各自部分）→ 有意重叠，只收超持仓的腿**；
            否则按最大余数法把持仓分摊到各腿。**不归并不同触发价**（每档止盈价
            是策略本体），也绝不再逐腿放大到全仓（那会把 N 腿炸成 N × 全仓）。
        返回动作摘要列表。

        **归属判不出来就什么都不做**（fail-closed）：原先的条件是
        `if self.label_prefix and not owned` —— 漏配 `label_prefix` 时整个条件为假，
        于是**任何**保护单都被当成自己的，会把别的 bot 的保护单撤掉重挂。
        """
        notes: list = []
        if not symbol:
            return notes
        tag = self._own_scope_tag(label)
        if not tag:
            log.warning("_resync_protectors(%s): skipped_unattributed —— "
                        "label_prefix 与 label 均为空，判不出本 bot 命名空间，"
                        "拒绝同步保护单张数（原先会退化成整表）", symbol)
            return [{"skipped_unattributed": 1, "reason": "no_namespace"}]
        positions = self._symbol_positions(symbol)
        if not positions:
            return notes
        total = sum(abs(float(p.get("size") or 0)) for p in positions)
        if total <= 0:
            return notes
        # 逐方向持仓量：SL 归并的目标张数按**该方向**算（双向持仓下不能拿 total）
        per_side: dict[str, float] = {}
        for p in positions:
            _s = str(p.get("side") or "")
            if _s:
                per_side[_s] = per_side.get(_s, 0) + qty(p.get("size"))
        try:
            rows = self.client.list_price_orders(symbol) or []
        except Exception:  # noqa: BLE001
            return notes

        sl_groups: dict[tuple[str, float], dict] = {}  # (方向, 触发价) → 归并组
        tp_group: list[dict] = []   # TP / limit 腿 → 按「组」对齐（见方法末）
        for p in rows:
            init = p.get("initial") or {}
            text = str(init.get("text") or p.get("text") or "")
            tail = text.rsplit("-", 1)[-1].lower() if text else ""
            if tail not in ("tp", "sl", "lp", "ls"):
                continue
            if not self._order_is_reduce_only(p):
                continue
            status = str(p.get("status") or "").lower()
            if status in ("cancelled", "finished", "filled", "triggered", "failed", "closed"):
                continue
            if not self._text_owned(text, tag):
                continue
            try:
                psz = float(init.get("size") or p.get("size") or 0)
            except (TypeError, ValueError):
                continue
            pid = self._order_id(p)
            trig = p.get("trigger") or {}
            trigger_price = trig.get("price") or init.get("trigger_price") or p.get("trigger_price")
            if not trigger_price:
                continue

            if tail in ("sl", "ls"):
                # 归并 SL：多单保护 size 为负、空单为正，**符号即方向**；
                # venue-computed 腿（size=0）改由 `auto_size` 定方向。
                side = _protector_side(init) or _protector_side(p) or (
                    "long" if psz < 0 else "short")
                if psz == 0:
                    # 「平该方向全部持仓」 → 等价张数就是该方向持仓量
                    psz = per_side.get(side, 0.0)
                try:
                    key = (side, round(float(trigger_price), 6))
                except (TypeError, ValueError):
                    continue
                g = sl_groups.setdefault(key, {
                    "ids": [], "size": 0.0, "px": str(trigger_price),
                    "rule": trig.get("rule"), "price_type": trig.get("price_type"),
                    "text": text or f"t-{tag}-sl",
                })
                if pid:
                    g["ids"].append(pid)
                g["size"] += abs(psz)
                if init.get("auto_size"):
                    g["auto_size"] = init.get("auto_size")
                continue

            # ── TP / limit 腿：收集，方法末按「组」对齐（_align_tp_group）──
            # 原逻辑逐腿 `|size| != 持仓 → 撤旧、放大到全仓`：单腿 TP（brooks 类）
            # 没问题，多级止盈会被炸成 N 腿 × 全仓（逐档 TP 2/2/2、持仓 4 →
            # 4/4/4 = 12 张，3 倍超配）。触发路径：reduce_long 落袋后 `_close` →
            # 本方法。网格每档有自己的止盈价 → 组对齐，绝不归并不同触发价。
            tp_group.append({
                "pid": pid, "psz": psz, "px": str(trigger_price),
                "rule": trig.get("rule"), "price_type": trig.get("price_type"),
                "text": text or f"t-{tag}-tp",
                # venue-computed 腿（size=0 + auto_size）：语义是「平该方向全部
                # 持仓」，不能参与逐档分摊（见 `_align_tp_group` 首段守卫）。
                "auto_size": init.get("auto_size"),
                # 该腿保护的方向：双向持仓下 TP 对齐**必须按方向分别算目标**，
                # 否则多头那 0.1 张的残仓会把空头 TP 的合计目标抬高到 16.2。
                "side": _protector_side(init) or _protector_side(p) or (
                    "long" if psz < 0 else "short"),
            })

        # ── SL 归并执行：一个 (方向, 触发价) 只留一条 ──
        for (side, _key_px), g in sl_groups.items():
            target = per_side.get(side, 0)
            if target <= 0:
                continue  # 该方向已无持仓 → 交给 _cleanup_orphan_protectors
            if len(g["ids"]) <= 1 and abs(g["size"] - target) < 0.5:
                continue  # 已是单条且张数正确 → 不动（避免无谓 churn）
            new_size = -target if side == "long" else target
            try:
                _init = {
                    "contract": symbol,
                    "size": new_size,
                    "price": "0",
                    "tif": "ioc",
                    "reduce_only": True,
                    "text": g["text"],
                }
                if g.get("auto_size") or self._venue_close_on():
                    # 归并后的那条 SL 也用 venue-computed 口径（不带张数）：
                    # `target` 是浮点真实持仓（16.1），触发单只收整数 → 会差 0.1 张。
                    _init["size"] = 0
                    _init["auto_size"] = ("close_long" if side == "long" else "close_short")
                body = {
                    "initial": _init,
                    "trigger": {
                        "rule": int(g["rule"] or (2 if side == "long" else 1)),
                        "price_type": int(g["price_type"] or 0),
                        "price": g["px"],
                    },
                }
                # **先挂新、后撤旧**：新单挂失败时旧单原样保留，保护不退化
                self.client.place_price_order(body)
                for pid in g["ids"]:
                    try:
                        self.client.cancel_price_order(pid)
                    except Exception:  # noqa: BLE001
                        pass
                notes.append({"merged_sl": g["text"], "side": side, "n": len(g["ids"]),
                              "from_size": g["size"], "to_size": target,
                              "trigger": g["px"], "size": new_size})
            except Exception as e:  # noqa: BLE001
                notes.append({"merged_sl_error": str(e), "text": g["text"]})
        # ── TP 腿组对齐：张数合计 == **该方向**持仓 → 逐档结构保持（零 churn）──
        # 按方向分组：双向持仓时用 `total`（两侧相加）当目标会把多头残仓的 0.1 张
        # 摊到空头 TP 上（16.1 → 16.2），越摊越偏。
        tp_by_side: dict[str, list[dict]] = {}
        for _leg in tp_group:
            tp_by_side.setdefault(str(_leg.get("side") or ""), []).append(_leg)
        for _side, _legs in tp_by_side.items():
            notes.extend(self._align_tp_group(symbol, _legs, per_side.get(_side, 0.0)))
        return notes

    def _size_step(self, symbol: str) -> float:
        """该合约的张数步长（取不到规格退回 1，即老行为）。"""
        try:
            meta = self.client.get_contract(symbol)
            step = float(getattr(meta, "size_step", 0) or 0)
        except Exception:  # noqa: BLE001
            return 1.0
        return step if step > 0 else 1.0

    def _venue_close_on(self) -> bool:
        """保护单是否走 venue-computed close（`size=0` + `auto_size`）。

        `account_risk.use_venue_close`，默认 **False**。只在「一个方向只有一条
        保护腿」的策略（单腿 brooks 类）上开：网格/马丁逐档 TP 各有自己的张数，
        一旦改成「平全方向」会把逐档结构炸掉。
        """
        return bool((self.account_risk or {}).get("use_venue_close"))

    def _align_tp_group(self, symbol: str, legs: list[dict], total: float) -> list:
        """把**多级 TP 腿组**的张数合计对齐到剩余持仓（逐档止盈结构保持）。

        背景：网格/马丁逐档止盈 = 多条同向 reduce-only TP 单、触发价各异、
        每腿张数是「该档自己那份」。减仓后旧逻辑逐腿 `|size| != total → 放大到
        全仓`，会把 2/2/2（持仓 6）炸成 6/6/6 = 18 张（3 倍超配，且先撤后挂
        中间还有保护空窗）。本方法按**组**处理：

        1. 组内张数合计 **== 持仓** → 什么都不做（稳态零 churn；这条同时
           保住了「同值不重挂」的纪律）。
        2. **恰好一条「全仓腿」（`|size| >= 持仓`）→ 有意重叠，不重分摊**：
           马丁共用 TP 覆盖全仓 + 逐档 TP 各自部分，合计天然 > 持仓。此时只把
           那条**超过持仓的腿**收到持仓（reduce-only 单不得超过持仓），其余各自
           部分腿原样不动 —— 若按 3 分摊，全仓腿会被挤成 `total×total/组合计`
           （如 29 → 23），整条出口结构被悄悄改小。
           （**多条**腿都 ≥ 持仓 = 陈旧腿堆叠（如持仓 1 却有两条 5 张），不是
           有意重叠 → 仍走 3 的分摊，避免「两条都收到 1 张」的双重覆盖。）
        3. 否则按**最大余数法**把 total 分摊到各腿（权重 = 各腿当前张数，
           0 张腿的结果可能是 0 → 撤单）。合计恒等于持仓、绝不超配。
        4. **先挂新、后撤旧**：挂不上的腿保留旧单（保护不退化），与 SL 归并
           同纪律。尺寸正好不变的腿 → 旧单即新单，不重挂。撤旧判据用**下标
           序号**而不是新单 id：客户端不回 id / id 意外撞号时，旧逻辑会漏撤
           旧单 → 双双 live = 双倍超配。
        """
        notes: list = []
        if not legs or total <= 0:
            return notes
        if any(l.get("auto_size") for l in legs):
            # venue-computed 腿（`size=0` + `auto_size`）已被交易所定义为
            # 「平该方向全部持仓」，没有可分摊的张数；混入分摊会把它的语义改坏。
            # 单腿 bot 的稳态就是「一条 venue TP + 一条 venue SL」→ 直接放行。
            return notes
        # 步长：ETH 0.1 / BTC 1。取不到规格就退回 1（老行为）。
        step = self._size_step(symbol)
        total = round(float(total), 8)
        group_sum = round(sum(qty(l["psz"]) for l in legs), 8)
        if abs(group_sum - total) < step / 2:
            return notes                      # 逐档结构保持 + 零 churn（含步长容差）
        full = [l for l in legs if qty(l["psz"]) >= total - 1e-9]
        if len(full) == 1 and len(legs) > 1:
            # 有意重叠：一条全仓腿（马丁共用 TP）+ 若干各自部分腿。
            # 只收超过持仓的那条，其余不动。
            l = full[0]
            sz = total
            body = {
                "initial": {
                    "contract": symbol,
                    "size": (-sz if l["psz"] < 0 else sz),
                    "price": "0", "tif": "ioc", "reduce_only": True,
                    "text": l["text"],
                },
                "trigger": {
                    "rule": int(l["rule"] or (2 if l["psz"] < 0 else 1)),
                    "price_type": int(l["price_type"] or 0),
                    "price": l["px"],
                },
            }
            try:
                self.client.place_price_order(body)   # 先挂新
                notes.append({"clamped": l["text"], "from": l["psz"],
                              "to": (-sz if l["psz"] < 0 else sz),
                              "trigger": l["px"]})
                pid = str(l["pid"] or "")             # 后撤旧
                if pid:
                    try:
                        self.client.cancel_price_order(pid)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as e:  # noqa: BLE001 — 挂不上不撤旧的
                notes.append({"resize_error": str(e), "text": l["text"]})
            return notes
        weights = [max(step, qty(l["psz"])) for l in legs]
        sizes = _apportion_sizes(total, weights, step)
        keep_old: set = set()                 # 下标 → 旧单保留（尺寸正好/挂新失败）
        for i, (l, sz) in enumerate(zip(legs, sizes)):   # 先挂新
            if sz <= 0:
                continue                      # 0 张腿 → 撤（见下）
            if abs(qty(l["psz"]) - sz) < 1e-9:
                keep_old.add(i)               # 尺寸正好 → 旧单即新单
                continue
            body = {
                "initial": {
                    "contract": symbol,
                    "size": (-sz if l["psz"] < 0 else sz),
                    "price": "0",
                    "tif": "ioc",
                    "reduce_only": True,
                    "text": l["text"],
                },
                "trigger": {
                    "rule": int(l["rule"] or (2 if l["psz"] < 0 else 1)),
                    "price_type": int(l["price_type"] or 0),
                    "price": l["px"],
                },
            }
            try:
                self.client.place_price_order(body)
                notes.append({"resized": l["text"], "from": l["psz"],
                              "to": (-sz if l["psz"] < 0 else sz),
                              "trigger": l["px"]})
            except Exception as e:  # noqa: BLE001 — 挂不上不撤旧的（保保护）
                notes.append({"resize_error": str(e), "text": l["text"]})
                keep_old.add(i)
        for i, l in enumerate(legs):          # 后撤旧
            if i in keep_old:
                continue
            pid = str(l["pid"] or "")
            if not pid:
                continue
            try:
                self.client.cancel_price_order(pid)
            except Exception:  # noqa: BLE001
                pass
        return notes

    def _owned_sl_size(self, symbol: str, prefix: str) -> float:
        """本 bot **未终结 SL 覆盖的张数合计**（绝对值）。

        只看 SL、不看 TP —— `watcher.reconcile_protection()` 的判据是
        `"-sl" in text or "-tp" in text`，也就是「只有 TP 没有 SL」会被判成已受保护。
        补保护这条路必须按 SL 单独判，不能沿用那个判据。

        **返回张数而不是布尔**：只看「有没有 SL」不够 —— 持仓 100 张而 SL 只覆盖
        10 张时会被判成「已受保护」而不补，留下静默的半裸仓。而这是真会发生的：
        `_resync_protectors()` 只在 `_close()` 里被调用一次（close/reduce 之后），
        **`add_*` 让持仓变大后没有任何地方会把 SL 放大** —— 而 `open_*`→`add_*`
        映射恰恰让持仓可以单调增长。
        """
        total = 0.0
        for po in self._owned_price_orders(symbol, prefix):
            text = str((po.get("initial") or {}).get("text") or po.get("text") or "")
            tail = text.rsplit("-", 1)[-1].lower() if text else ""
            if tail not in ("sl", "ls"):
                continue
            if str(po.get("status") or "").lower() in (
                "cancelled", "finished", "filled", "triggered", "failed", "closed"
            ):
                continue
            init = po.get("initial") or {}
            try:
                total += qty(init.get("size") or po.get("size"))
            except (TypeError, ValueError):
                continue
        return total

    def _owned_sl_orders(self, symbol: str) -> list[tuple[str, float, float]]:
        """本 bot 未终结的 SL 价格单 → `[(order_id, trigger_price, 张数), ...]`。

        与 `_owned_sl_size` **同源**（都走 `_owned_price_orders` + `-sl/-ls` 后缀
        + 未终结状态过滤），保证「补保护」与「上移保护」两条路径看到的是同一批单 ——
        判据漂移是这一族代码反复出问题的地方。张数取绝对值（多单的保护单 size 为负）。
        """
        return [(pid, px, sz) for pid, px, sz, _s in self._owned_sl_orders_sided(symbol)]

    def _owned_sl_orders_sided(self, symbol: str) -> list[tuple[str, float, float, str]]:
        """同 `_owned_sl_orders`，但多带一个 `side`（这条 SL 保护的是哪个方向）。

        **双向持仓必须靠它分腿**：保护单是 reduce-only，平多的 size 为负、平空为正，
        符号即方向。若用「触发价相对 mark 的位置」去判，会在**回撤已经越过 SL** 时
        误判 —— 那一刻 SL 恰好跑到 mark 的另一侧，正是最需要认对它的时候。
        """
        out: list[tuple[str, float, int, str]] = []
        # venue-computed 腿（`size=0`）覆盖的是「该方向全部持仓」——物化成当前
        # 真实张数再返回，下游「保护合计 vs 持仓」的算式才不会失真（否则 0 张
        # 会被读成「没有保护」→ 每轮重挂）。按需取数，无 venue 腿时零额外开销。
        cov: Optional[dict[str, float]] = None

        def _coverage() -> dict[str, float]:
            nonlocal cov
            if cov is None:
                cov = {}
                try:
                    for p in self._symbol_positions(symbol):
                        cov[p["side"]] = cov.get(p["side"], 0.0) + abs(p["size"])
                except Exception as e:  # noqa: BLE001
                    swallow("executor._owned_sl_orders_sided.coverage", e,
                            level=logging.DEBUG)
                    cov = {}
            return cov

        for po in self._owned_price_orders(symbol, self._own_prefix("")):
            init = po.get("initial") or {}
            text = str(init.get("text") or po.get("text") or "")
            tail = text.rsplit("-", 1)[-1].lower() if text else ""
            if tail not in ("sl", "ls"):
                continue
            if str(po.get("status") or "").lower() in (
                "cancelled", "finished", "filled", "triggered", "failed", "closed"
            ):
                continue
            pid = self._order_id(po)
            trig = po.get("trigger") or {}
            px = trig.get("price") or init.get("trigger_price") or po.get("trigger_price")
            try:
                px = float(px)
            except (TypeError, ValueError):
                continue
            if not pid or px <= 0:
                continue
            try:
                raw_sz = _num(init.get("size") or po.get("size"))
            except (TypeError, ValueError):
                raw_sz = 0
            side = _protector_side(init) or _protector_side(po)
            if side is None:
                continue
            if raw_sz == 0:
                # venue-computed：张数由交易所算，等价于「该方向全部持仓」
                raw_sz = _coverage().get(side, 0.0)
                if raw_sz == 0:
                    continue  # 该方向已无持仓 → 交给孤儿清理
            else:
                side = "long" if raw_sz < 0 else "short"
            out.append((str(pid), px, abs(raw_sz), side))
        return out

    def _resolve_sl_size(self, intent, pos_side: str, exit_size: float,
                         meta) -> tuple[float, list[str]]:
        """算出 SL 该挂多少张 + 要撤掉的同价旧单（F1）。

        三步：
          ① `sl_scope: shared` 的末层带 `sl_size_override`（= 全梯计划总量）→
             **直接取它**，与 `tp_size_override` 对称。此前该字段**写了没人读**，
             「全梯共用一条 SL」只能靠 ② 的同价归并逐档累加间接达成。
          ② `_plan_shared_sl` 的同价归并（把旧同价单并进来 + 返回要撤的 id）。
          ③ 若用了 ①，再钳制到**真实敞口**：`_plan_shared_sl` 只在「有同价旧单」
             分支钳制，而末层先跑 / 前几档 SL 没挂上时 override 可能大于实际持仓 ——
             这里兜底，保证**永不挂出超过敞口的 reduce_only 保护单**。

        非 override 路径（`sl_scope: per_level` 与所有普通开仓）**逐字不变**：
        第 ③ 步只在 ① 生效时执行。
        """
        override = float(intent.sl_size_override) if intent.sl_size_override else 0.0
        # 小数张（ETH 0.1 步长）：`int(0.7)` = 0 → 首档挂出一条 **0 张 SL**
        # → paper 引擎 `0.0 or None` → float(None) → ExchangeError('invalid size')，
        # 且交易所侧也是一条无意义 0 张单。全程 float，步长交给 paper/validate 落。
        sl_size = override or round(float(exit_size), 8)
        merge_ids: list[str] = []
        if intent.sl_mode != "limit_order":
            try:
                sl_size, merge_ids = self._plan_shared_sl(
                    intent.symbol, pos_side, float(intent.sl), float(sl_size), meta)
            except Exception as e:  # noqa: BLE001 — 归并只是优化，失败不该阻断挂 SL
                log.warning("shared-SL merge probe failed %s: %s", intent.symbol, e)
                merge_ids = []
        if override:
            try:
                cap = self._sl_cover_cap(intent.symbol, pos_side)
                if cap > 0:
                    sl_size = min(float(sl_size), float(cap))
            except Exception as e:  # noqa: BLE001
                swallow("executor._resolve_sl_size.cap", e, level=logging.WARNING)
        return round(float(sl_size), 8), merge_ids

    def _plan_shared_sl(self, symbol: str, pos_side: str, new_sl: float,
                        add_size: float, meta) -> tuple[float, list[str]]:
        """为「全梯共用一条 SL」算出新 SL 应挂的张数，以及要撤掉的同价旧单。

        阶梯网格是**逐档**调 `_open` 建仓的（每档一张或几张），每档都带自己的
        `intent.sl`。若照搬「每档挂一条 size = 本档」，盘上会累积出 N 条同价 SL
        （实测 martin-grid 4 条 `t-mtg-sl`，sz 9/1/1/6 —— 人格的设计却是
        **全梯共用一条**，见 `prompts/ladder_nogate.md` §六）。而 `_modify_tp_sl`
        路径挂的本来就是「一条 size = 持仓总量」，两条路径口径不一致正是分腿来源。

        这里把**同向同价**的已有 SL 归并进来：新单张数 = 同价旧单张数之和 +
        本档计划张数；旧单由调用方在新单**落盘后**再撤（先挂后撤，不留空窗）。

        - 返回 `(size, cancel_ids)`。
        - 只归并同价的**：人格 §4.3 的「远端档自带止损」落在不同价位，属于
          独立仓位，**不在**归并范围 —— 靠价格比较把两类分开。
        - 容差取 1 个最小变动价位（`meta.order_price_round`）；取不到时退化成
          精确相等（宁可少归并、不可错并）。
        - 张数**钳制在真实敞口内**（`_sl_cover_cap`）：逐档建仓时 `old_total`
          恰好等于「前几档累加」，正常路径下 `old_total + add` 正好等于上限、
          不会被削；只有旧 SL 被外部放大过（如历史 `peak_trail` 遗留）才会触发
          钳制 —— 那时按真实敞口重挂才是对的。**永不挂出超过敞口的 protect 单**：
          Gate 若因 reduce_only 超配而拒，`_open` 会走 rollback 把入场单也撤了。
        """
        # 小数张：`int(0.7)`=0 会把「本档 + 同价旧单归并」算成 0 张 SL（裸仓）
        add_size = round(float(add_size), 8)
        ids: list[str] = []
        old_total = 0.0
        try:
            tol = abs(float(getattr(meta, "order_price_round", 0) or 0))
        except (TypeError, ValueError):
            tol = 0.0
        target = round_price(float(new_sl), meta)
        for pid, px, sz, side in self._owned_sl_orders_sided(symbol):
            if side != pos_side:
                continue
            if abs(round_price(px, meta) - target) > tol + 1e-9:
                continue
            ids.append(pid)
            old_total += _num(sz)
        if not ids:
            return add_size, []          # 没有同价旧单 → 本档单挂（原行为）
        out_size = min(old_total + add_size, self._sl_cover_cap(symbol, pos_side))
        if out_size <= 0:
            return add_size, []
        return out_size, ids

    def _sl_cover_cap(self, symbol: str, pos_side: str) -> float:
        """该方向「SL 应当覆盖」的张数上限 = 已成交持仓 + 已挂未成交入场单。

        给同价归并定一个不越界的上限（见 `_plan_shared_sl`）。入场单只算
        **同向**：买单（size>0）建多头、卖单建空头，与保护单 reduce-only 的
        符号方向一致。普通单的 `left` = 未成交张数。

        **排除「本轮将被撤掉的旧梯」**（`_round_replaced_order_ids`）：`place_before_cancel`
        期间新旧梯同时在场，不排除就会把旧梯算进敞口 → SL 多算一整梯
        （产线实测 2026-10-14：持仓 5 + 挂单 26 → SL 57 而非 31，且因为管理轮有
        no-op churn 守卫、铺单轮 cap 同样是放大值，57 会一直卡住不回落）。
        该集合只由 `execute_signal` 在 `replace_mode != none` 时登记，其余时刻为空集
        = 行为逐字不变。

        查不到（API 抖动）时 `_owned_open_orders` 返回空 → 上限退化成持仓量 →
        归并被削到「只覆盖已成交」= 安全侧（少并、不多挂）。
        """
        pos = 0.0
        for p in self._symbol_positions(symbol):
            if str(p.get("side") or "") == pos_side:
                pos += qty(p.get("size"))
        replaced = getattr(self, "_round_replaced_order_ids", None) or set()
        pend = 0
        for o in self._owned_open_orders(symbol, self._own_prefix("")):
            if str(o.get("status") or "").lower() not in ("open", ""):
                continue
            try:
                size = _num(o.get("size"))
                left = _num(o.get("left"))
            except (TypeError, ValueError):
                continue
            if size == 0 or left <= 0:
                continue
            if str(o.get("id") or "") in replaced:
                continue          # 本轮即将撤掉的旧梯 → 不算进敞口
            if (size > 0) == (pos_side == "long"):
                pend += left
        return pos + pend

    def ensure_protection(self, symbol: str) -> dict:
        """裸仓兜底：持仓在、owned SL 不在时，按配置补一张 reduce_only SL。

        生产里此前**没有任何代码会补** —— `watcher.reconcile_protection()` 只返回警告
        （且全仓唯一调用者是上线前测试脚本），`_orphan_sweep` 只撤孤儿不补。于是两条
        路径都会留下裸仓，只能等 AI 下一轮自己发现：
          ① 入场成交了但保护单没挂上（`_rollback_unprotected_entry` 只告警不平仓）
          ② SL 被触发/被撤销，而仓位还在

        只补 SL，**绝不补 TP**：SL 是风控，TP 是策略 —— 自动塞一个 TP 等于替 AI 做了
        它没做的决策，还会和它下一轮的意图打架。

        **按覆盖张数判，不按「有没有 SL」判**（`_owned_sl_size`）：判成「已受保护」
        的门槛是 `covered >= size`（只有 TP 没有 SL 不算受保护）。但覆盖不足时
        **不按差额补** —— 见下面「不变量」注释：差额补单会让 SL 触发时只平掉
        一部分，那是「程序减仓」，本仓明确禁止。

        由 `account_risk` 逐 bot 开启（默认关）：
          auto_protect: false（默认）→ 只回报检测结果，不下单
          auto_protect: "dry"        → 记录「本来会挂什么」，不下单
          auto_protect: true         → 真挂
          auto_protect_sl_pct: 2.0   → SL 距 mark 的百分比；≤0 视为未启用

        价格锚 **mark**（不锚入场价）：仓位已经很赚时按入场价算出来的价会落在 mark 的
        非法一侧、被交易所直接拒 —— 正是 `_precheck_exit_triggers` 挡的那一类。这里再
        走一次同一个预检，避免两处判据漂移。

        **故意不看「有没有待成交入场单」**（`_has_pending_entry`）。两个理由：
          ① 那个判据会静默返回 False —— `62dce02`（漏扫条件单）和 `b0307ab`（漏判空头
             `left` 为负）连着修了两次漏判。而「补保护」是**必须动**的路径：一个可能漏判
             的判据不该给它当闸门，否则漏判的代价是「裸仓一直没人管」。
          ② 它多余：未成交入场单的保护单保护的是**将来**那条仓位，与当前这条无关；
             而能走到下面挂单那一步，就说明覆盖张数不足（`covered < size`），
             本 bot 现有的 owned SL 覆盖不了这条持仓 —— 不会双挂。

        本方法只做「检测 + 挂单」，**不告警**：告警与限流是调用方（watcher 的扫描循环）
        的策略，那里才有跨轮状态。
        """
        out: dict[str, Any] = {"symbol": symbol, "auto_protect": "off"}
        # 持仓管理总闸：只开仓不管仓的 bot 不补任何保护（补 SL 也是管仓）
        if not self.manage_positions:
            out["skipped"] = "manage_positions_off"
            return out
        ar = self.account_risk or {}
        mode = ar.get("auto_protect", False)
        try:
            pct = float(ar.get("auto_protect_sl_pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if not mode or pct <= 0:
            return out
        dry = str(mode).lower() == "dry"
        out["auto_protect"] = "dry" if dry else "live"

        positions = self._symbol_positions(symbol)
        if not positions:
            out["skipped"] = "no_position"
            return out
        if len(positions) > 1:
            # 双向持仓得先定补哪条腿 —— 这不是「兜底」该猜的事，交给 AI
            out["skipped"] = "ambiguous_side"
            return out

        pos = positions[0]
        pos_side = pos["side"]
        size = abs(_num(pos["size"]))
        covered = self._owned_sl_size(symbol, self._own_prefix(""))
        if covered >= size:
            out["skipped"] = "sl_present"
            return out
        # ── 不变量：程序挂出的 SL 张数**恒等于持仓全量** ──
        # 触发即「全出」= 止损；不存在「程序平掉一部分」这种动作。
        # 所以这里**绝不按缺口补单**：`size - covered` 是**内部覆盖记账**的口径，
        # 把它直接当下单张数用（旧版 `size=need`），SL 触发时就只平掉差额张 ——
        # 既不是全平、也不是结构减仓，没有任何交易理由，纯粹是记账泄漏成了
        # 仓位动作（用户 2026-10-13 拍板：「不许程序减仓」）。
        #
        # 部分覆盖（0 < covered < size）只可能是异常：SL 被外部撤掉一半、或
        # `add_*` 让持仓变大而 SL 没跟上（`_resync_protectors()` 只在 `_close()`
        # 里跑）。异常**不猜、不补**，报出来交回 AI / 人工。
        if covered > 0:
            out.update({
                "position_side": pos_side, "position_size": size,
                "covered_size": covered, "shortfall": size - covered,
                "skipped": "partial_coverage",
            })
            # 观察期（含 dry）要能看到「本来会挂什么」——否则 dry 验证不了任何东西
            try:
                ticker = self.client.get_ticker(symbol) or {}
                mark = float(ticker.get("mark_price") or ticker.get("last") or 0)
            except Exception:  # noqa: BLE001
                mark = 0.0
            if mark > 0:
                meta = self.client.get_contract(symbol)
                raw = (mark * (1 - pct / 100.0) if pos_side == "long"
                       else mark * (1 + pct / 100.0))
                out.update({"mark": mark, "sl": float(round_price(raw, meta)),
                            "sl_pct": pct})
            return out
        # 走到这里 covered == 0：完全裸仓 → 挂**全量** SL（与旧版行为一致）
        # 持仓不足 1 张（小数张残仓）：`_place_trigger` 会自动改用 venue-close
        # （`auto_size`，不带张数），所以这里不再跳过 —— 跳过等于把残仓留在裸奔状态。
        try:
            ticker = self.client.get_ticker(symbol) or {}
            mark = float(ticker.get("mark_price") or ticker.get("last") or 0)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"no mark price: {e}"
            return out
        if mark <= 0:
            out["error"] = "no mark price"
            return out

        meta = self.client.get_contract(symbol)
        raw = mark * (1 - pct / 100.0) if pos_side == "long" else mark * (1 + pct / 100.0)
        sl = float(round_price(raw, meta))
        out.update({
            "position_side": pos_side, "position_size": size,
            "covered_size": covered, "need_size": size,
            "mark": mark, "sl": sl, "sl_pct": pct,
        })
        if dry:
            return out

        intent = Intent(
            action="open_long" if pos_side == "long" else "open_short",
            symbol=symbol, side=pos_side, sl=sl,
            sl_type="market",  # 触发即市价：兜底止损要的是「一定出得来」
            label=self.label_prefix or "auto",
        )
        intent.trigger_rule_sl = infer_trigger_rules(intent.action, False)
        side_err = self._precheck_exit_triggers(intent)
        if side_err:
            out["error"] = side_err
            return out

        trigger_side = "short" if pos_side == "long" else "long"
        try:
            rec, err = self._place_exit_leg(
                lambda: self._place_trigger(
                    # 张数恒 = 持仓全量（见上面「不变量」）：触发即全出，不是减仓
                    intent, trigger_side, sl, is_tp=False, meta=meta, size=size
                ),
                kind="sl",
                price_order=True,
            )
        except Exception as e:  # noqa: BLE001 — boundary
            rec, err = None, str(e)
        if not rec:
            out["error"] = f"place failed: {err}"
            return out
        order = rec.get("order") or {}
        out["placed"] = {"id": str(order.get("id") or ""), "price": sl, "check": rec.get("check")}
        return out

    def ensure_grid_tp(self, symbol: str) -> dict:
        """阶梯档「**条件限价**止盈」执行器 —— **AI 定计划，程序只执行**。

        背景（2026-10-15 用户拍板）：入场腿在挂单时只能带**占位 TP**（多单 TP 触发价
        必须 > mark，而档位目标在现价下方 → 非法，挂不上）。档位成交后目标价才合法。
        本方法在**成交后**把 AI 已经写进计划的目标价（`meta.tp_target`，落盘在
        `ladder_levels.json`）执行成一张 **conditional-limit reduce_only TP**
        （trigger = 目标价、限价 = 目标价、tif=gtc）—— 即「条件限价平仓单」。

        **边界（硬约束）**：程序**绝不自己造价格**。`tp_target` 缺失 → 跳过并留痕，
        交回 AI（与 `ensure_protection` 的「只补 SL、绝不补 TP」同源精神：TP 是策略，
        不是风控兜底；这里只执行 AI 已经写下的目标，不替它决策）。

        由 `account_risk.grid_tp_auto` 逐 bot 开启（默认关）：
          false/"off"（默认）/None → 不动作
          "dry"                    → 只回报「本来会挂什么」，不下单
          true/"live"              → 真挂
        """
        out: dict[str, Any] = {"symbol": symbol, "grid_tp": "off",
                               "candidates": [], "placed": [], "errors": [], "skipped": []}
        if not self.manage_positions:
            out["skipped"] = "manage_positions_off"
            return out
        ar = self.account_risk or {}
        mode = ar.get("grid_tp_auto", "off")
        if not mode or str(mode).lower() in ("off", "false", "none"):
            return out
        dry = str(mode).lower() == "dry"
        out["grid_tp"] = "dry" if dry else "live"
        if not self.bot_id:
            out["skipped"] = "no_bot_id"
            return out

        path = (Path(self.root) / "data" / "bots" / self.bot_id
                / "state" / "ladder_levels.json")
        if not path.exists():
            out["skipped"] = "no_ladder_table"
            return out
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            out["skipped"] = "ladder_table_corrupt"
            return out
        levels_all = [lv for lv in (data.get("levels") or [])
                      if isinstance(lv, dict) and lv.get("symbol") == symbol]

        # 持仓与在盘 TP —— **覆盖校验的输入**。原先只看「档位表 status==pending」，
        # 于是 TP 被撤（对账/重铺）后那一档永久失去止盈，且没有「在盘 TP 合计 <
        # 持仓」的兜底（实测 2026-10-15：档位 2423.7 的逐档 TP 挂出 4 分钟即被撤，
        # 表里仍标 protected，从此不再补挂）。
        # 用 `_positions_or_error`（而非 `_symbol_positions`）：后者「取不到就当没有」，
        # 会把接口抖动读成空仓 → 本方法就会把 filled 档全判成闭环。宁可不动。
        raw_pos, perr = self._positions_or_error()
        if raw_pos is None:
            out["skipped"] = "positions_unavailable"
            out["error"] = f"持仓查询失败: {perr}"
            return out
        positions = _parse_symbol_positions(raw_pos, symbol, meta=self._safe_meta(symbol))
        # ⚠️ **2026-10-16 事故修复（严重）**：覆盖口径必须是「**多头持仓**」，
        # 原来用 Σ|size| —— 空仓也会被算成「需要覆盖的敞口」，于是程序继续挂
        # 卖单，越挂越空（实盘：−14 张空单，22:51~05:01 累积）。
        # 单向模式下 size>0 = 多、size<0 = 空，方向由符号唯一决定。
        pos_long = 0
        pos_short = 0
        for p in positions or []:
            # 小数张（ETH 0.7）：`_int_or_zero` 会截成 0 → 误判「无多头持仓」
            n = _num(p.get("size"))
            if n > 0:
                pos_long += n
            elif n < 0:
                pos_short += -n
        pos = pos_long
        out["position_long"] = pos_long
        out["position_short"] = pos_short
        # ── 反向仓处置（模式自适应，默认只告警）────────────────────────────
        # 本策略只做多 → 反向 = 空。single 下是**净空仓**、dual 下是**独立空腿**，
        # 但 `close_position` 自己按模式分支，这里**同一套判据**。
        # 为什么放在这里：单向模式下空仓会让 `no_flip` 拦死所有 `open_long`，
        # 而人格只做多、不会自己发 `close_short` → 网格永久停摆（实盘 2026-10-16
        # 停摆 4 小时）。`stray_position: flatten` 时由程序平掉，形成自愈路径。
        # 归属证据：档位表存在即算（`-gtp` 卖单会被下面的硬闸撤掉，单靠订单判归属
        # 会「撤完就没证据」）。
        out["stray"] = self.flatten_stray(symbol, pos_short,
                                          owned=bool(levels_all) or None)
        if out["stray"].get("action") == "flattened":
            # IOC 市价平仓 → 本次扫描按「已无反向仓」继续（下一轮以交易所为准复核）
            pos_short = 0
            out["position_short"] = 0
        # 在盘普通挂单（入场腿）id —— 供「台账 ↔ 交易所对齐」判 pending 档死活
        open_entry_ids: set = set()
        try:
            for o in (self._owned_open_orders(symbol, self._own_prefix("")) or []):
                oid = str(self._order_id(o) or "")
                if oid:
                    open_entry_ids.add(oid)
        except Exception as e:  # noqa: BLE001
            swallow("executor.ensure_grid_tp.open_entry", e, level=logging.DEBUG)
        # ── 在盘止盈卖单集合（普通限价单 + reduce_only，text 后缀 -gtp）────
        # **形态（2026-10-16 事故后定稿）**：止盈 = **普通限价卖单 + reduce_only=True**。
        # 三种形态实测（无持仓）：条件触发单 reduce_only → 400 AUTO_TRIGGER_PRICE_
        # GREATE_MARK；普通限价 reduce_only → 400 INCREASE_POSITION；普通限价**无标记**
        # → 能挂上，但**无持仓成交即开空**（2026-10-15 22:51~10-16 05:01 实盘累积
        # −14 张空单，长仓人格随后被 no_flip 拦死 12 轮）→ **无标记卖单形态被否决**。
        # 定稿：**只在有多头持仓时挂**（下面 ⛔ 硬闸）+ **必须 reduce_only**
        # （交易所保证只减多、永不反手；持仓消失时交易所会自动撤该单）。
        open_gtp_ids: set = set()
        existing: set = set()
        covered_size = 0
        try:
            for o in (self._owned_open_orders(symbol, self._own_prefix("")) or []):
                if not str(o.get("text") or "").endswith("-gtp"):
                    continue
                oid = str(self._order_id(o) or "")
                if oid:
                    open_gtp_ids.add(oid)
                existing.add(self._price_key(o.get("price")))
                covered_size += abs(_num(o.get("size")))
        except Exception as e:  # noqa: BLE001
            swallow("executor.ensure_grid_tp.gtp_orders", e, level=logging.DEBUG)
            out["skipped"] = "orders_unavailable"
            return out
        open_ids = open_gtp_ids
        out["position"] = pos
        out["covered"] = covered_size
        state_fixes: list = []
        # mark：候选过滤的基准（target <= mark 的档挂了会立即成交 → 开空）
        mark = _to_float((self.client.get_ticker(symbol) or {}).get("mark_price"))
        if mark <= 0:
            out["skipped"] = "mark_unavailable"
            return out
        out["mark"] = mark

        # ── ⓪ 台账 ↔ 交易所对齐：pending 档的入场单是否还在盘 ────────────
        # 重铺/撤单后旧档的入场单已被撤，但表里仍标 pending（实测 5 小时从 30 涨到
        # 110 条）→ 表污染、LLM 快照被死档占据。以**交易所为准**纠正：
        # 在盘 → 保持 pending；已成交 → filled；已撤/查不到 → 删除该行。
        # 每轮限制查询条数（老档优先），避免病态表把一轮扫描拖死。
        pruned, promoted, checked = 0, 0, 0
        for lv in list(levels_all):
            if checked >= _LADDER_PRUNE_MAX:
                break
            if str(lv.get("status")) != "pending":
                continue
            oid = str(lv.get("order_id") or "")
            if not oid or oid in open_entry_ids:
                continue
            checked += 1
            try:
                o = self.client.get_order(oid) or {}
            except Exception as e:  # noqa: BLE001
                if _is_order_gone(e):
                    # 交易所侧已无此单 → 该档已死，**删行**。原先统一按「查不到 → 不动」
                    # 处理，于是死档只增不减（见 `_is_order_gone` docstring）。
                    levels_all.remove(lv)
                    lst = data.get("levels") or []
                    if lv in lst:                 # 不用 try/except（审计 B12 裸吞棘轮）
                        lst.remove(lv)
                    pruned += 1
                    continue
                swallow("executor.ensure_grid_tp.prune_get_order", e, level=logging.DEBUG)
                continue                      # 真·抖动 → 不动（fail-closed）
            if str(o.get("finish_as") or "") == "filled":
                lv["status"] = "filled"
                promoted += 1
            else:
                levels_all.remove(lv)
                lst = data.get("levels") or []
                if lv in lst:                 # 不用 try/except（审计 B12 裸吞棘轮）
                    lst.remove(lv)
                pruned += 1
        if pruned or promoted:
            out["ladder_pruned"] = pruned
            out["ladder_promoted"] = promoted
            state_fixes.append({"ladder_sync": {"pruned": pruned, "promoted": promoted}})

        # ── ① 台账维护：protected 档的止盈卖单（普通限价单）是否还在盘/已成交 ──
        # 普通单终态：`finish_as=filled` = 已成交（该档止盈落袋）→ closed；
        # `status=open` 但不在集合（理论不该发生）→ 以单查为准不动；
        # 其余（cancelled/未知）→ 退回 pending 重挂。
        # ⚠️ 无持仓时**不再整体闭环**：普通卖单不依赖持仓而存在（预挂形态，
        # 用户要求的「完全挂完」），只有卖单本身成交才闭环。
        for lv in levels_all:
            if str(lv.get("status")) != "protected":
                continue
            tid = str(lv.get("tp_order_id") or "")
            if tid and tid in open_ids:
                continue                      # 卖单还在盘 → 保持 protected
            if not tid:
                lv["status"] = "pending"
                state_fixes.append({"price": lv.get("price"), "to": "pending",
                                    "why": "no_tp_order_id"})
                continue
            try:
                o = self.client.get_order(tid) or {}
            except Exception as e:  # noqa: BLE001
                if _is_order_gone(e):
                    # 止盈单已不存在（撤单/被清理）→ 退回 pending 并清 `tp_order_id`，
                    # 让本轮候选循环**立刻重挂**。原先卡在 protected 不动，而候选只取
                    # pending/filled → 这一档从此没有止盈（见 `_is_order_gone`）。
                    lv["status"] = "pending"
                    lv.pop("tp_order_id", None)
                    state_fixes.append({"price": lv.get("price"), "to": "pending",
                                        "why": "gtp_order_gone"})
                    continue
                swallow("executor.ensure_grid_tp.get_order", e, level=logging.DEBUG)
                continue                      # 真·抖动 → 不动（fail-closed）
            if str(o.get("finish_as") or "") == "filled":
                lv["status"] = "closed"       # 卖单已成交 → 该档生命周期结束
                state_fixes.append({"price": lv.get("price"), "to": "closed",
                                    "finish_as": "filled"})
            elif str(o.get("status") or "") == "open":
                continue                      # 在盘（集合未抓到，以单查为准）
            else:
                lv["status"] = "pending"
                lv.pop("tp_order_id", None)
                state_fixes.append({"price": lv.get("price"), "to": "pending",
                                    "why": f"gtp_{str(o.get('finish_as') or o.get('status') or 'gone')}"})
        out["state_fixes"] = state_fixes

        # ── ⛔ 硬闸（2026-10-16 事故修复）：**无多头持仓 → 一张止盈卖单都不挂** ──
        # 单向模式下卖单是**方向性订单**：有持仓时成交 = 平多止盈；**无持仓时成交 = 开空**。
        # 实测事故（2026-10-15 22:51 → 10-16 05:01）：预挂的普通限价卖单在无多头时
        # 陆续成交 → 累积成 **−14 张空单** → 长仓人格随后被 `no_flip` 拦死，连续 12 轮
        # 失败（"持有 short 时不能直接开 long"），网格停摆 4 小时。
        # 结论：**「无持仓预挂止盈卖单」在单向模式下等价于「预挂开空单」，不可能安全**
        # （reduce_only 无仓会被拒；条件单同理）。所以止盈只能在**有多头之后**挂，
        # 并强制 `reduce_only`（双保险：即使仓位变化，卖单也永不反手）。
        # 顺带把在盘 -gtp 卖单撤掉（它们此刻就是"挂着的开空单"）。
        if pos_long <= 0:
            cancelled = []
            if not dry:
                for oid in list(open_gtp_ids):
                    try:
                        self.client.cancel_order(oid)
                        cancelled.append(oid)
                    except Exception as e:  # noqa: BLE001
                        swallow("executor.ensure_grid_tp.cancel_orphan_gtp", e,
                                level=logging.DEBUG)
            if pos_short > 0:
                log.warning("grid tp %s: 账户持有 %g 张**空仓**（非本策略方向）→ "
                            "不挂止盈、撤掉在盘 -gtp 卖单 %d 条；请先平掉空仓",
                            symbol, pos_short, len(cancelled))
            out["skipped"] = [{"why": "no_long_position(单向卖单=开空，禁止无仓预挂)",
                               "position_long": pos_long, "position_short": pos_short,
                               "cancelled_gtp": len(cancelled)}]
            if (cancelled or state_fixes) and not dry:
                self._persist_ladder(path, data)
            return out

        # ── ② 候选：pending 网格档中「目标价 > mark」的**全部**档 ───────────
        # **不再要求该档已成交**（单向净仓簿：先挂上 = 用户的「完全挂完」；
        # 之后任何持仓的反弹卖出都由这些卖单承接）。target <= mark 跳过 ——
        # 低于市价的限价卖单会立即以市价成交（= 直接开空）。
        candidates, skipped = [], []
        # 档位张数是小数张时不许按整数取（0.7 → 0 会整档挂不上）
        _gtp_step = safe_step(self.client.get_contract(symbol))
        for lv in levels_all:
            if str(lv.get("status")) not in ("pending", "filled"):
                continue
            tgt = lv.get("tp_target")
            if not tgt:
                skipped.append({"price": lv.get("price"), "why": "no_tp_target(交回AI)"})
                continue
            tf = _to_float(tgt)
            if tf <= 0:
                skipped.append({"price": lv.get("price"), "why": "bad_target"})
                continue
            if tf <= mark:
                skipped.append({"price": lv.get("price"), "why": f"below_mark({mark})"})
                continue
            if self._price_key(tgt) in existing:
                skipped.append({"price": lv.get("price"), "why": "tp_already_present"})
                continue
            # 张数上限 = 该档张数 ∩ 「多头持仓 − 在盘 TP 已覆盖」的剩余敞口。
            # 绝不超配：reduce_only 卖单张数超过持仓时，多余部分会被交易所拒/撤。
            remaining = pos_long - covered_size
            size = min(qty(lv.get("size")) or _gtp_step, max(remaining, 0))
            if size <= 0:
                skipped.append({"price": lv.get("price"),
                                "why": f"long_fully_covered({pos_long})"})
                continue
            covered_size += size          # 递增，防多档重复覆盖同一份持仓
            candidates.append({"level": lv, "target": tf, "size": size})

        out["candidates"] = [{"price": c["level"].get("price"), "tp": c["target"],
                              "size": c["size"]} for c in candidates]
        out["skipped"] = skipped
        if not candidates:
            if state_fixes and not dry:
                self._persist_ladder(path, data)
            return out
        if dry:
            return out

        meta = self.client.get_contract(symbol)
        placed, errors, done_idx = [], [], []
        for c in candidates:
            lv, tgt, size = c["level"], c["target"], c["size"]
            if size <= 0:
                errors.append({"price": lv.get("price"), "error": "zero_size"})
                continue
            # **普通限价卖单 + `reduce_only=True`**（2026-10-16 事故修复）：
            # 卖单是方向性订单 —— 无 reduce_only 时，无多头成交即开空（实盘已发生
            # −14 张空单）。加 reduce_only 后交易所保证它**只能减少多头**，永远不会
            # 反手开空；持仓消失时交易所会自动撤掉该单（官方文档：「reduce_only 单
            # 系统可能自动撤单以确保持仓不增加」）。
            try:
                order = self.client.place_order({
                    "contract": symbol, "size": -size,
                    "price": str(round_price(tgt, meta)),
                    "tif": "gtc",
                    "reduce_only": True,
                    "text": f"t-{self._bot_tag(self.label_prefix or 'auto')}-gtp",
                })
            except Exception as e:  # noqa: BLE001 — boundary
                errors.append({"price": lv.get("price"), "tp": tgt,
                               "error": str(e)[:200]})
                continue
            placed.append({"price": lv.get("price"), "tp": tgt, "size": size,
                           "order_type": "limit", "reduce_only": True,
                           "id": str((order or {}).get("id") or "")})
            lv["tp_order_id"] = str((order or {}).get("id") or "")
            lv["status"] = "protected"
            done_idx.append(lv)

        # 落盘：已挂出的档标 protected（并记 TP 单 id）；台账状态变更同样落盘。
        # `levels_all` 是 `data["levels"]` 的**引用**，上面的就地改动已生效。
        if done_idx or state_fixes:
            self._persist_ladder(path, data)
        out["placed"] = placed
        out["errors"] = errors
        return out

    @staticmethod
    def _price_key(v: Any) -> str:
        """触发价归一化键（去空白/去尾零）—— 刻意不做 float()，避免为解析新增裸吞异常。"""
        if v is None:
            return ""
        s = str(v).strip()
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or ""

    @staticmethod
    def _persist_ladder(path: Path, data: dict) -> None:
        """档位表原子落盘（失败只留痕，不拦主流程）。"""
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:  # noqa: BLE001
            swallow("executor._persist_ladder", e, level=logging.WARNING)

    # ── 峰值回撤保护（防坐电梯）──────────────────────────
    def _peak_trail_path(self) -> Path:
        """峰值状态的位置：**恒为 bot 级**（`data/bots/<bot_id>/state/peak_trail.json`）。

        为什么不用账户级：`peak_trail` 是**逐 bot 的开关**，而状态按账户共享时，
        同一账户下开了这个开关的 bot 会互相覆盖对方的峰值记录 —— 一个 bot 的
        扫描把另一条腿的 peak 写坏，下一轮它就按错的基准挪止损。
        保护单本身仍按 `label_prefix` 命名空间隔离（`_owned_sl_orders`），
        所以「各算各的峰值、只挪自己的 SL」是自洽的；需要跨 bot 协同的是
        `ensure_protection` 那条路，不是这条。
        """
        return (Path(self.root) / "data" / "bots" / (self.bot_id or "_unknown")
                / "state" / "peak_trail.json")

    def _read_peak_trail(self) -> dict:
        """读 `{ "SYMBOL|side": {peak, entry, size, ts} }`。读不到返回空 —— 
        「没有峰值」= 重新起算 = 不动作，是安全侧。"""
        try:
            import json as _json

            p = self._peak_trail_path()
            if not p.exists():
                return {}
            rec = _json.loads(p.read_text(encoding="utf-8"))
            return dict(rec.get("positions") or {})
        except Exception:  # noqa: BLE001
            return {}

    def _write_peak_trail(self, positions: dict) -> None:
        try:
            import json as _json

            p = self._peak_trail_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(
                {"positions": positions, "updated": int(time.time())},
                ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ── 浮盈回撤保护（give_back_pct）─────────────────────────
    def _give_back_path(self) -> Path:
        """浮盈回撤状态：恒为 bot 级（与 `peak_trail` 同口径）。"""
        if self.bot_id:
            return (self.root / "data" / "bots" / str(self.bot_id)
                    / "state" / "give_back.json")
        return self.root / "state" / "give_back.json"

    def _read_give_back(self) -> dict:
        """读 `{"positions": {"SYMBOL|side": {...}}}`。读不到返回空 ——
        「没有峰值」= 重新起算 = 不动作，是安全侧。"""
        try:
            import json as _json

            p = self._give_back_path()
            if not p.is_file():
                return {}
            rec = _json.loads(p.read_text(encoding="utf-8"))
            return dict(rec.get("positions") or {})
        except Exception:  # noqa: BLE001 — 状态坏了当没有，不阻塞扫描
            return {}

    def _write_give_back(self, state: dict) -> None:
        try:
            import json as _json

            p = self._give_back_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps({"positions": state}, ensure_ascii=False,
                                       indent=1), encoding="utf-8")
            tmp.replace(p)
        except Exception:  # noqa: BLE001
            pass

    def remember_give_back(self, symbol: str, side: str, pct: Any) -> None:
        """把**计划给的**浮盈回撤阈值记进状态（开仓 / 挂突破单时调用）。

        为什么要这一步：`give_back_pct` 是**逐计划**的（模型每笔自己算），而扫描
        循环是**逐 bot** 的 —— 它不知道当前持仓是哪一轮、按什么阈值开的。
        所以开仓那一刻把值落到 `symbol|side` 上，扫描时按腿取。
        """
        try:
            v = float(pct) if pct is not None else 0.0
        except (TypeError, ValueError):
            return
        if v <= 0 or v >= 100:
            return
        state = self._read_give_back()
        key = f"{symbol}|{side}"
        rec = dict(state.get(key) or {})
        rec["pct"] = v
        state[key] = rec
        self._write_give_back(state)

    def check_give_back(self, symbol: str, dry: bool = False) -> dict:
        """浮盈从峰值回撤 `pct`% → 平掉该仓位（阈值取自 `remember_give_back`）。

        与 `check_peak_trail` 的区别：那个**上移 SL**（防坐电梯），这个**直接平仓**
        （落袋）。两者都挂在扫描循环上，不依赖 LLM 轮次。

        **开关**：由 `account_risk.give_back`（off/dry/live，默认 off）逐 bot 单独
        控制（watcher 的 `_give_back_sweep` 入口前置判断）；`manage_positions=false`
        时一并熄火。**不受 `allow_close` 影响** —— 那条开关管的是「AI 每轮当场发的
        close 指令」，本路径是「AI 开仓时预先授权、程序后台执行」，性质不同。

        **武装条件：峰值浮盈 ≥ 1R**（R = |入场价 − 本 bot 的 SL| × 张数 × quanto）。
        这等价于「TP1 之后才武装」——TP1 就设在 1R。没有这条，峰值只有 0.1R 时
        回撤 40% 也会平，而锁定的那点利润还不够付往返手续费。

        **平仓条件（三条都要满足）**：
          ① 已武装
          ② 当前浮盈 ≤ 峰值浮盈 × (1 − pct/100)
          ③ **当前浮盈 > 0** —— 保本平仓，绝不亏着平

        峰值**必须重置**的情况（与 `peak_trail` 同口径）：持仓消失 / 方向反转 /
        入场价变了 / 上次观测太久远（`_GIVE_BACK_STALE_SEC`）。
        """
        out: dict[str, Any] = {"symbol": symbol, "give_back": "off", "sides": []}
        out["give_back"] = "dry" if dry else "live"
        # 闸门：只受**总闸** manage_positions 管（`manage_positions=false` = 只开仓
        # 不管仓，语义上包含它）。本路径自己的开关是 `account_risk.give_back`
        # （off/dry/live，默认 off，由 watcher 的 `_give_back_sweep` 入口前置判断）。
        #
        # **不受 `allow_close` 影响**（2026-10-13 解绑）：那条开关管的是「AI 每轮
        # 当场发的 close 指令」，而本路径是「AI 开仓那一刻预先授权阈值、程序在 60s
        # 扫描里执行」—— 性质不同，不该被同一个开关连带熄火（绑错会导致「想放开
        # AI 平仓却顺带关掉了它早已授权的回调锁盈」）。
        if not self.manage_positions:
            out["skipped"] = "manage_positions_off"
            # 不管仓 = 也不该留一张会自己平仓的常驻触发单（关功能后的回收）
            out["cleared"] = self.clear_give_back(symbol)
            return out

        positions = self._symbol_positions(symbol)
        state = self._read_give_back()
        if not positions:
            # 持仓没了 → 清掉该 symbol 的峰值（留着会让下次开仓一上来就「已回撤」）
            stale = [k for k in state if k.split("|", 1)[0] == symbol]
            if stale:
                for k in stale:
                    state.pop(k, None)
                self._write_give_back(state)
            out["skipped"] = "no_position"
            # 仓没了 → 常驻锁盈单必须一并撤掉：reduce-only 单在无仓时触发会被交易所
            # 拒绝，但留在盘上会挡住归属判断（且持仓再回来时价位已无意义）
            out["cancelled"] = self.cancel_give_back_orders(symbol)
            return out

        for pos in positions:
            out["sides"].append(self._give_back_side(pos, symbol, state, dry))
        live = {f"{symbol}|{p['side']}" for p in positions}
        for k in [k for k in state if k.split("|", 1)[0] == symbol and k not in live]:
            state.pop(k, None)
        self._write_give_back(state)
        if out["sides"]:
            for f in ("position_side", "position_size", "peak_pnl", "cur_pnl",
                      "r_usd", "armed", "closed", "skipped", "error",
                      "trigger_pnl", "gb_level", "gb"):
                if f in out["sides"][0]:
                    out[f] = out["sides"][0][f]
        return out

    def _give_back_side(self, pos: dict, symbol: str, state: dict,
                        dry: bool) -> dict:
        side = str(pos.get("side") or "")
        size = qty(pos.get("size"))
        entry = float(pos.get("entry_price") or 0)
        key = f"{symbol}|{side}"
        out: dict[str, Any] = {"position_side": side, "position_size": size}

        rec = dict(state.get(key) or {})
        try:
            pct = float(rec.get("pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct <= 0 or pct >= 100:
            out["skipped"] = "no_pct"
            # 没有授权阈值 → 不该有常驻锁盈单（关功能 / 换策略后的残留要清掉）
            self.cancel_give_back_orders(symbol, side=side)
            return out

        try:
            ticker = self.client.get_ticker(symbol) or {}
            mark = float(ticker.get("mark_price") or ticker.get("last") or 0)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"no mark price: {e}"
            return out
        if mark <= 0 or entry <= 0 or size <= 0:
            out["error"] = "no mark price"
            return out

        meta = self.client.get_contract(symbol)
        quanto = float(getattr(meta, "quanto_multiplier", 0) or 0)
        if quanto <= 0:
            out["error"] = "no quanto_multiplier"
            return out
        # 浮盈（USD）：多单 (mark−entry)、空单 (entry−mark)，乘张数与合约乘数
        cur_pnl = ((mark - entry) if side == "long" else (entry - mark)) * size * quanto
        out["cur_pnl"] = round(cur_pnl, 4)

        # 1R：用**本 bot 的 SL** 算。SL 被上移过就是更小的 R（风险已收紧），
        # 这是想要的语义。没有 owned SL 就没法定义 R → 跳过。
        sls = [(pid, px, sz) for pid, px, sz, s in self._owned_sl_orders_sided(symbol)
               if s == side]
        if not sls:
            out["skipped"] = "no_owned_sl"
            return out
        best_sl = (max(p for _, p, _ in sls) if side == "long"
                   else min(p for _, p, _ in sls))
        r_usd = abs(entry - best_sl) * size * quanto
        out["r_usd"] = round(r_usd, 4)
        if r_usd <= 0:
            out["skipped"] = "no_risk"
            return out

        peak = float(rec.get("peak_pnl") or 0)
        same = (
            rec.get("peak_pnl") is not None
            and (time.time() - float(rec.get("ts") or 0)) <= _GIVE_BACK_STALE_SEC
            and abs(float(rec.get("entry") or 0) - entry) <= max(abs(entry), 1.0) * 1e-6
        )
        peak = max(peak, cur_pnl) if same else cur_pnl
        armed = bool(rec.get("armed")) if same else False
        if not armed and peak >= r_usd:
            armed = True                       # 峰值浮盈够 1R → 武装
        state[key] = {"peak_pnl": peak, "armed": armed, "entry": entry,
                      "size": size, "pct": pct, "ts": time.time()}
        out["peak_pnl"] = round(peak, 4)
        out["armed"] = armed

        if not armed:
            out["skipped"] = "not_armed"
            return out
        trigger = peak * (1 - pct / 100.0)
        out["trigger_pnl"] = round(trigger, 4)
        # 锁盈价位：空单在入场价下方、多单在上方，随峰值单向收紧
        level = self._gb_level(entry, trigger, size, quanto, side, meta)
        out["gb_level"] = round(level, 8)
        if cur_pnl > trigger:
            # 还没回撤到阈值 → 把锁盈价挂成**交易所常驻触发单**（方案 3）：
            # 60s 轮询间隙与进程死亡期都不再失明 —— 触发由交易所执行，零延迟。
            out["skipped"] = "no_retrace"
            out["gb"] = self._ensure_give_back_trigger(
                symbol, side, level, size, meta, dry, entry=entry)
            return out
        if cur_pnl <= 0:
            # 已回撤到盈亏平衡以下 —— 此时平仓是「止损」，那是 SL 的职责。
            # 这条路径只做「保本平仓」，不越权当止损用。常驻锁盈单同样要撤：
            # 它一旦触发就是在盈亏线下方成交，违背「绝不亏着平」。
            out["skipped"] = "not_profitable"
            self.cancel_give_back_orders(symbol, side=side)
            return out
        if dry:
            out["skipped"] = "dry"
            return out
        # 本路径要自己市价平 → 先撤常驻单，避免同一仓位被两条路径重复平
        self.cancel_give_back_orders(symbol, side=side)
        try:
            order = self.client.close_position(symbol, side=side, size=0) or {}
        except GateApiError as e:
            out["error"] = str(e)[:160]
            return out
        state.pop(key, None)                   # 平掉了 → 清峰值
        # 已实现盈亏：优先取**交易所回报**（含手续费/资金费，与 `_close` 同口径），
        # 交易所没回 `pnl` 时退回引擎按 mark 推算的值 —— 用 `pnl_source` 标注来源，
        # 复盘时能区分「账面推算」与「交易所实结」。
        pnl, pnl_source = cur_pnl, "engine"
        if order.get("pnl") is not None:
            try:
                pnl, pnl_source = float(order["pnl"]), "venue"
            except (TypeError, ValueError):
                pnl, pnl_source = cur_pnl, "engine"
        out["closed"] = {"order_id": order.get("id"), "pnl": round(pnl, 4),
                         "peak_pnl": round(peak, 4), "size": size,
                         "pnl_source": pnl_source}
        return out

    # ── 方案 3：把锁盈位挂成交易所常驻触发单 ────────────────────────
    def _give_back_text(self) -> str:
        """本 bot 的锁盈触发单 text（同一命名空间，`gb` 尾巴与 tp/sl 区分）。"""
        return f"t-{self._bot_tag(self.label_prefix or 'auto')}-{_GIVE_BACK_TAIL}"

    @staticmethod
    def _gb_row_view(po: dict) -> dict:
        """触发单的两种返回形态统一成一个视图。

        Gate 是嵌套 `{initial:{size,text}, trigger:{price}}`，paper 引擎是平铺
        （`size`/`text`/`trigger_price`）—— 两个都要认，否则纸面验证看不出问题，
        一上实盘全部读空。
        """
        init = po.get("initial") or {}
        trig = po.get("trigger") or {}
        text = str(init.get("text") or po.get("text") or "")
        px = trig.get("price") or init.get("trigger_price") or po.get("trigger_price") or 0
        try:
            px = float(px)
        except (TypeError, ValueError):
            px = 0.0
        raw_sz = init.get("size")
        if raw_sz is None:
            raw_sz = po.get("size")
        try:
            sz = float(raw_sz or 0)
        except (TypeError, ValueError):
            sz = 0.0
        return {
            "id": str(po.get("id") or po.get("order_id") or ""),
            "text": text, "px": px, "size": sz,
            "status": str(po.get("status") or "").lower(),
        }

    def _give_back_orders(self, symbol: str = "", side: str = "") -> list[dict]:
        """本 bot 在盘上的锁盈触发单（`gb` 尾巴）。

        `symbol` 为空 = 全账户（用于关功能时的一次性回收）；`side` 用于双向持仓
        分腿（触发单带符号：负=卖平多、正=买平空，与 SL 同口径）。
        """
        prefix = self._own_prefix("")
        if not prefix:
            return []
        label = prefix[2:] if prefix.startswith("t-") else prefix
        try:
            if symbol:
                rows = self.client.list_price_orders(symbol) or []
            else:
                rows = self.client.list_price_orders() or []
        except Exception as e:  # noqa: BLE001 — 查不到就当没有，不阻塞平仓主路径
            swallow("executor._give_back_orders", e, level=logging.DEBUG)
            return []
        out: list[dict] = []
        for po in rows:
            v = self._gb_row_view(po)
            if v["status"] in _DEAD_PO_STATUS:
                continue
            if v["text"].rsplit("-", 1)[-1].lower() != _GIVE_BACK_TAIL:
                continue
            if not self._text_owned(v["text"], label):
                continue
            if side and ((v["size"] < 0) != (side == "long")):
                continue
            out.append(v)
        return out

    def cancel_give_back_orders(self, symbol: str = "", side: str = "") -> list[str]:
        """撤掉本 bot 的锁盈触发单（幂等）。返回实际撤掉的 id。"""
        cancelled: list[str] = []
        for o in self._give_back_orders(symbol, side):
            if not o["id"]:
                continue
            try:
                self.client.cancel_price_order(o["id"])
                cancelled.append(o["id"])
            except Exception:  # noqa: BLE001 — 已被触发/撤掉的单撤不动是正常结局
                pass
        return cancelled

    def clear_give_back(self, symbol: str = "") -> dict:
        """撤单 + 清峰值状态（关功能 / 平仓 / 无阈值时用）。"""
        ids = self.cancel_give_back_orders(symbol)
        state = self._read_give_back()
        drop = [k for k in state
                if not symbol or k.split("|", 1)[0] == symbol]
        for k in drop:
            state.pop(k, None)
        if drop:
            self._write_give_back(state)
        return {"cancelled": ids, "cleared": len(drop)}

    @staticmethod
    def _gb_level(entry: float, trigger_pnl: float, size: float, quanto: float,
                  side: str, meta: Any) -> float:
        """锁盈触发价 = 入场价 ∓ 触发浮盈/(张数×乘数)，并对齐到价格步长。

        对齐方向刻意取**保守侧**（空单向上取整、多单向下取整）：宁可少锁一点利润，
        也不能让触发价落进现价内侧 —— 那会在挂上去的瞬间就成交，等于乱平仓。
        """
        per_pt = size * quanto
        if per_pt <= 0:
            return 0.0
        delta = trigger_pnl / per_pt
        raw = (entry - delta) if side == "short" else (entry + delta)
        tick = abs(float(getattr(meta, "order_price_round", 0) or 0))
        if tick <= 0:
            return raw
        px = (math.ceil(raw / tick) if side == "short" else math.floor(raw / tick)) * tick
        precision = max(0, int(round(-math.log10(tick))) if tick < 1 else 0)
        return float(f"{px:.{precision}f}")

    def _ensure_give_back_trigger(self, symbol: str, side: str, level: float,
                                  size: float, meta: Any, dry: bool,
                                  entry: float = 0.0) -> dict:
        """确保交易所上挂着「价格到 `level` 就减仓离场」的 reduce-only 触发单。

        为什么（方案 3）：60s 扫描间隙和进程死亡期，`give_back` 都是盲的 —— 价格
        在窗口里穿过锁盈位没人动作。触发单驻留在交易所，**触发不依赖本进程**，
        等于把锁盈从「软件轮询」升级成「交易所执行」，延迟归零。

        **幂等 + 单向棘轮**：
          - 已有一条价位相同、张数正确的 → 什么都不做（零 churn）
          - 已有单比目标**锁得更多**（空单更低 / 多单更高）→ 保留它，绝不为了对齐
            一个更松的目标去「放宽」已锁住的利润（峰值 900s 过期重置后会算出更松的
            目标价，这条是那处的兜底闸门）
          - 峰值创新高 → 价位往更有利方向挪：**先挂新、后撤旧**（挂失败时旧单仍在，
            保护不退化 —— 与 SL 归并 `_resync_protectors` 同序）

        **price_type 必须是 mark(1)**：本方法的算术用的是 `mark_price`，若触发基准
        用最新成交价，两个基准不同源会在插针/价差时提前或推迟触发。
        """
        signed = int(-size if side == "long" else size)   # 负=卖平多，正=买平空
        info: dict[str, Any] = {"level": round(float(level), 8), "size": signed}
        if signed == 0:
            # price_orders 只收整数张：不足 1 张挂 0 张单等于没有保护，直说
            return {"action": "skip", "reason": "size_below_1", **info}

        rows = self._give_back_orders(symbol, side=side)
        tick = abs(float(getattr(meta, "order_price_round", 0) or 0))
        tol = tick if tick > 0 else max(abs(float(level)), 1.0) * 1e-9
        # 「像本次持仓的价位」的容忍带：以目标价到入场价的距离为尺度。带外的单
        # 只可能是上一笔交易的残留（同一仓位不会锁出这么远的价），一律换掉。
        span = max(abs(float(level) - float(entry or level)) * 1.5, tol * 2)

        reusable = None
        for r in rows:
            if abs(r["size"]) != abs(signed):
                continue
            if entry and abs(r["px"] - float(entry)) > span:
                continue
            tighter = ((r["px"] <= float(level) + tol) if side == "short"
                       else (r["px"] >= float(level) - tol))
            if tighter:
                reusable = r
                break
        if reusable is not None:
            for r in rows:                       # 只留一条，多余的撤掉
                if r["id"] and r["id"] != reusable["id"]:
                    try:
                        self.client.cancel_price_order(r["id"])
                    except Exception:  # noqa: BLE001
                        pass
            return {"action": "kept", "order_id": reusable["id"],
                    "level": round(float(reusable["px"]), 8), **info}
        if dry:
            return {"action": "dry", **info}

        body = {
            "initial": {
                "contract": symbol,
                "size": signed,
                "price": "0",             # 触发后走市价（IOC）成交，不做限价赌运气
                "tif": "ioc",
                "reduce_only": True,      # 兜底：挂反/无仓时交易所直接拒绝
                "text": self._give_back_text(),
            },
            "trigger": {
                "rule": 1 if side == "short" else 2,   # 空单涨破 / 多单跌破
                "price_type": 1,                       # mark，与本方法算术同源
                "price": f"{float(level):.8f}".rstrip("0").rstrip("."),
            },
        }
        try:
            placed = self.client.place_price_order(body)      # 先挂新
        except Exception as e:  # noqa: BLE001 — 挂不上就保住旧单，如实回报
            return {"action": "error", "error": str(e)[:160], **info}
        for r in rows:                                        # 再撤旧
            if r["id"]:
                try:
                    self.client.cancel_price_order(r["id"])
                except Exception:  # noqa: BLE001
                    pass
        return {"action": "placed" if not rows else "moved",
                "order_id": str((placed or {}).get("id") or ""), **info}

    def check_peak_trail(self, symbol: str) -> dict:
        """单持仓**价格峰值回撤** → 上移 SL（`account_risk.peak_trail` 逐 bot 开，默认关）。

        为什么需要它：
          - 止损锚定的是**挂单那一刻给定的价**，挂上去就不再动。价格涨上去再跌
            回来它管不了 —— 那正是「坐电梯」。
          - 交易所侧的移动止盈（`trail`）在本项目**搁置**（需资金密码，paper 侧
            直接不支持），所以「让交易所自己往上挪止损」这条路是关着的。
          - 账户级权益熔断（`equity_deviation_halt`）只挡新开仓，防不住浮盈回吐。

        口径（**逐仓**、与入场价解耦 —— 所以浮盈仓和浮亏仓一视同仁）：
          - 峰值 = 该持仓存续期间**最有利的价格**（多单取最高、空单取最低），只上不下
          - 触发距离 = `peak_trail_atr` × ATR%（`_atr_pct`，1h 周期）
          - 目标 SL = 峰值 ∓ 距离；**只往更保守的方向挪**（多单更高、空单更低）
          - 真动手时沿用 `_modify_tp_sl` 的既定顺序：**先挂新、再撤旧**

        三条**不动作**的红线（都不下单，只回报原因）：
          ① `sl_not_better`：目标不比现有 owned SL 更保守。这也覆盖了「ATR 变大
             导致目标下移」的情况 —— 于是 SL 单调不降，不会来回抖。
          ② `trail_breached`：目标落在 mark 的**非法一侧**，说明这次回撤已经发生
             过了，此刻挂单会立刻触发（= 变相市价平仓）。那是一个**不同**的动作，
             不该由「上移止损」这条路径偷偷做掉，所以只报不挂。
          ③ `no_owned_sl`：没有本 bot 的 SL，交给 `ensure_protection`（补保护那条路）。

        峰值**必须重置**的四种情况（否则会拿旧高点去卡一条新仓）：
          持仓消失 / 方向反转 / 入场价变了 / 上次观测太久远（`_PEAK_TRAIL_STALE_SEC`）。
        """
        out: dict[str, Any] = {"symbol": symbol, "peak_trail": "off", "sides": []}
        # 持仓管理总闸：上移 SL 属于管仓 —— 只开仓不管仓的 bot 不挪任何止损
        # （allow_close/allow_reduce 不适用：它不动敞口，只改保护价）。
        if not self.manage_positions:
            out["skipped"] = "manage_positions_off"
            return out
        ar = self.account_risk or {}
        mode = ar.get("peak_trail", False)
        if not mode:
            return out
        try:
            mult = float(ar.get("peak_trail_atr") or 0)
        except (TypeError, ValueError):
            mult = 0.0
        if mult <= 0:
            return out
        dry = str(mode).lower() == "dry"
        out["peak_trail"] = "dry" if dry else "live"
        out["atr_mult"] = mult

        positions = self._symbol_positions(symbol)
        state = self._read_peak_trail()
        if not positions:
            # 持仓没了 → **必须清掉该 symbol 的峰值**。留着的话下次开同一标的会
            # 拿上一轮的高点当基准，一开仓就「已经回撤很多」→ 立刻把新仓卡掉。
            stale = [k for k in state if k.split("|", 1)[0] == symbol]
            if stale:
                for k in stale:
                    state.pop(k, None)
                self._write_peak_trail(state)
            out["skipped"] = "no_position"
            return out
        # 双向持仓：**两条腿各自跟踪峰值、各自上移自己的 SL**。
        # 旧版在这里 `ambiguous_side` 直接返回 —— 而「永远双向」的阶梯策略
        # 恰恰长期双向，等于这条保护对它完全失效。key 本来就是 `symbol|side`，
        # 状态天然按方向分开，所以逐腿处理不需要额外记账。
        out["sides"] = []
        for _pos in positions:
            out["sides"].append(self._peak_trail_side(_pos, symbol, state, mult, dry))
        live = {f"{symbol}|{p['side']}" for p in positions}
        for _k in [k for k in state if k.split("|", 1)[0] == symbol and k not in live]:
            state.pop(_k, None)
        self._write_peak_trail(state)
        if out["sides"]:
            for _f in ("position_side", "position_size", "peak", "mark", "atr_pct",
                       "target_sl", "current_sl", "moved", "skipped", "error"):
                if _f in out["sides"][0]:
                    out[_f] = out["sides"][0][_f]
        return out

    def _peak_trail_side(self, pos: dict, symbol: str, state: dict,
                         mult: float, dry: bool) -> dict:
        """单条腿：峰值回撤 → 上移该腿的 SL。判据与旧版逐字相同。

        `state` 由调用方持有并统一落盘（逐腿只改自己那个 `symbol|side` 键）。
        """
        side = pos["side"]
        size = abs(_num(pos["size"]))
        entry = float(pos.get("entry_price") or 0)
        key = f"{symbol}|{side}"
        out: dict[str, Any] = {"position_side": side, "position_size": size}

        try:
            ticker = self.client.get_ticker(symbol) or {}
            mark = float(ticker.get("mark_price") or ticker.get("last") or 0)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"no mark price: {e}"
            return out
        if mark <= 0:
            out["error"] = "no mark price"
            return out

        rec = dict(state.get(key) or {})
        peak = float(rec.get("peak") or 0)
        # 同一条持仓的判据：方向相同（key 已含）+ 入场价一致 + 上次观测没过期。
        # 入场价因加仓而变也算「换了持仓」（保守：重新起算，不会误触发）；
        # 观测过期说明连续性断了（进程停过 / 扫描没跑到），历史高点不可信。
        same = (
            peak > 0
            and (time.time() - float(rec.get("ts") or 0)) <= _PEAK_TRAIL_STALE_SEC
            and abs(float(rec.get("entry") or 0) - entry) <= max(abs(entry), 1.0) * 1e-6
        )
        if same:
            peak = max(peak, mark) if side == "long" else min(peak, mark)
        else:
            # 新起算：以当前 mark 为起点，**不追认**我们没观测到的历史高点
            peak = mark
        # 方向反转：同一 symbol 的**旧方向**峰值必须清掉。留着的话反手回来时
        # key 又变回旧方向，会直接复用一个早已过期的历史高点。
        # （双向同时持仓的情况上面已经 `ambiguous_side` 返回了，走不到这里。）
        # 不再在这里清理「同 symbol 其他方向」—— 双向持仓下两条腿都要保留，
        # 由调用方按「当前实际持仓」统一清理并落盘。
        state[key] = {"peak": peak, "entry": entry, "size": size, "ts": time.time()}

        atr_pct = self._atr_pct(
            symbol, int((self.account_risk or {}).get("peak_trail_atr_period") or 14))
        if atr_pct <= 0:
            out["error"] = "no atr"
            return out
        meta = self.client.get_contract(symbol)
        dist = peak * atr_pct / 100.0 * mult
        target = float(round_price(peak - dist if side == "long" else peak + dist, meta))
        out.update({
            "position_side": side, "position_size": size,
            "peak": peak, "mark": mark, "atr_pct": round(atr_pct, 4),
            "target_sl": target,
        })

        # 双向持仓下两条腿的 SL 都会返回 —— 按保护单自身的方向挑出这一腿的
        # （不能用「相对 mark 的位置」判：回撤越过 SL 时它已经跑到另一侧了）。
        sls = [(pid, px, sz) for pid, px, sz, s in self._owned_sl_orders_sided(symbol)
               if s == side]
        if not sls:
            out["skipped"] = "no_owned_sl"
            return out
        best_sl = (max(s for _, s, _ in sls) if side == "long"
                   else min(s for _, s, _ in sls))
        out["current_sl"] = best_sl

        if not (target > best_sl if side == "long" else target < best_sl):
            out["skipped"] = "sl_not_better"
            return out
        if not (target < mark if side == "long" else target > mark):
            out["skipped"] = "trail_breached"
            return out
        if dry:
            return out

        intent = Intent(
            action="open_long" if side == "long" else "open_short",
            symbol=symbol, side=side, sl=target,
            sl_type="market",  # 触发即市价：落袋要的是「一定出得来」
            label=self.label_prefix or "auto",
        )
        intent.trigger_rule_sl = infer_trigger_rules(intent.action, False)
        side_err = self._precheck_exit_triggers(intent)
        if side_err:
            out["error"] = side_err
            return out

        trigger_side = "short" if side == "long" else "long"
        try:
            placed, perr = self._place_exit_leg(
                lambda: self._place_trigger(
                    intent, trigger_side, target, is_tp=False, meta=meta, size=size
                ),
                kind="sl",
                price_order=True,
            )
        except Exception as e:  # noqa: BLE001 — boundary
            placed, perr = None, str(e)
        if not placed:
            # **先挂新、再撤旧**：挂失败时旧 SL 还在，仓位不会裸 —— 这正是这个顺序
            # 唯一的意义。反过来的话挂失败就留下裸仓（`_modify_tp_sl` 同此约定）。
            out["error"] = f"replace failed: {perr}"
            return out

        new_id = str((placed.get("order") or {}).get("id") or "")
        cancelled: list[str] = []
        for pid, _, _ in sls:
            if pid == new_id:
                continue
            try:
                self.client.cancel_price_order(pid)
                cancelled.append(pid)
            except Exception:  # noqa: BLE001
                pass
        out["moved"] = {"from": best_sl, "to": target, "size": size,
                        "id": new_id, "cancelled": cancelled}
        return out

    def _close(self, intent: Intent) -> StepResult:
        self._check_symbol(intent.symbol)
        dual = self.client.is_dual_position_mode()
        requested = (intent.meta or {}).get("requested_action") or intent.action
        side = intent.side
        if requested == "reduce_long":
            side = "long"
        elif requested == "reduce_short":
            side = "short"
        if dual and side not in ("long", "short"):
            raise GateApiError(
                "dual position mode requires side (use reduce_long / reduce_short or side=long|short)"
            )
        size = intent.close_size
        if size is not None and size <= 0:
            raise GateApiError("reduce size must be positive")
        order = self.client.close_position(
            intent.symbol,
            side=side if dual else side,
            size=size or 0,
        )
        # 平/减仓后：先撤孤儿，再把剩余保护单张数对齐持仓
        _lbl = getattr(intent, "label", "") or ""
        cleaned = self._cleanup_orphan_protectors(intent.symbol, label=_lbl)
        resized = self._resync_protectors(intent.symbol, label=_lbl)
        # 统一字段名：与 open_* 的 detail 对齐，供卡片/日志等消费者直读
        _o = order or {}
        _px = _o.get("fill_price") or _o.get("avg_price") or _o.get("price")
        _pnl = _o.get("pnl")
        detail: dict[str, Any] = {
            "order": order,
            "side": side,
            "position_mode": self.client.get_position_mode(),
            "executed_as": "close",
            "cleaned_protectors": cleaned,
            "resized_protectors": resized,
            "mode_note": (
                "dual: close this side only" if dual else "single: one book, side is advisory"
            ),
        }
        if _px not in (None, "", "0"):
            detail["entry_price"] = _px
        if _pnl is not None:
            detail["realized_pnl"] = _pnl
        return StepResult(
            requested,
            intent.symbol,
            True,
            detail=detail,
        )

    def _close_all(self, symbol: str, scope: str = "") -> StepResult:
        """市价全平。

        `symbol` 为空时必须显式 `scope`（`bot` / `account`）—— 原先「空 symbol = 全账户」
        是**静默**的。而且宽路径下**只平本 bot 有归属的仓**：归属判不了（该合约上没有
        本 bot 命名空间的挂单/条件单）就不平它，落进 `skipped_unattributed` ——
        这是「平掉别的 bot 的仓」的唯一入口（审计 C-3/C-16）。
        带 symbol 的路径**逐字不变**（单币 bot 全走这条）。
        """
        wide = not symbol
        if wide:
            scope = self._wide_scope(scope, "close_all")
        symbols = [symbol] if symbol else self._wide_symbols(scope, self._open_symbols())
        if symbol:
            self._check_symbol(symbol)
        orders = []
        skipped_unattributed: list[str] = []
        dual = self.client.is_dual_position_mode()
        for sym in symbols:
            # **不要在这里再过滤白名单**：`scope=bot` 的收窄已经由 `_wide_symbols`
            # 做过，而 `scope=account` 的语义就是「账户上本 bot 的仓」—— 再套一层白名单
            # 会让 account **退化成 bot**（S2.3#2/D5 承诺的「能力不丢」逃生口失效）。
            # 归属判据只在 `scope=account` 需要：那个作用面跨 bot，光有白名单不足以说明
            # 「这个合约上的仓是我的」。单币路径由 `_check_symbol` 把关。
            if wide and scope == "account" and not self._symbol_owned(sym):
                # 判不了归属 → 不平（fail-closed）。跳过的仓要留痕（见下面 detail）。
                skipped_unattributed.append(sym)
                log.warning("close_all: %s 无本 bot 归属证据 → 跳过不平（fail-closed）", sym)
                continue
            if dual:
                for side in ("long", "short"):
                    try:
                        oid = self.client.close_position(sym, side=side).get("id")
                        if oid is not None:
                            orders.append(oid)
                    except GateApiError as e:
                        if _is_flat_error(e):
                            continue
                        raise
            else:
                try:
                    oid = self.client.close_position(sym, side=None).get("id")
                    if oid is not None:
                        orders.append(oid)
                except GateApiError as e:
                    if _is_flat_error(e):
                        continue
                    raise
        detail: dict[str, Any] = {"closed_order_ids": orders}
        if wide:
            detail["scope"] = scope
            detail["skipped_unattributed"] = skipped_unattributed
            if not orders and skipped_unattributed:
                # **一个都没平** = 这次 close_all 没生效，不能报成功。原先恒 `ok=True`：
                # 归档里看起来是成功、而账户上的仓一个没动 —— 静默 no-op 比报错危险，
                # 因为没人会去查一个「成功」的动作。
                return StepResult(
                    "close_all", symbol, False, detail=detail,
                    error=(f"close_all({scope}): 候选 {len(skipped_unattributed)} 个合约"
                           f"全部无本 bot 归属证据 → 未平任何仓 {skipped_unattributed}；"
                           f"若这些仓确实是本 bot 的，请检查 label_prefix 与本 bot 的挂单"))
        return StepResult("close_all", symbol, True, detail=detail)

    def _cancel_all(self, symbol: str, label: str = "", scope: str = "") -> StepResult:
        """撤销本 bot 的普通挂单（**带持仓感知**）。

        与 `cancel_price_all` 同理：`cancel_all` 同样是钝动作，持仓判断错一次就会
        连带撤掉保护。持仓数据取不到 → 拒绝；正在保护真实持仓的 reduce-only 挂单 → 跳过。
        `symbol` 为空时必须显式 `scope`（原先静默放大到全账户）。
        """
        wide = not symbol
        if wide:
            scope = self._wide_scope(scope, "cancel_all")
        prefix = self._own_prefix(label)
        # own-scope isolation for bot namespace / explicit label.
        # **判不出命名空间就拒绝**：原先 `own_tag in ("", "signal")` 会**静默退回整表撤单**
        # （`cancel_all_orders(symbol)`）—— 那会撤掉别的 bot 在同一合约上的单（审计 C-9）。
        # `own_tag == "signal"` 现在是**可确定**的本 bot 命名空间（默认 label = t-signal*），
        # 所以走 own 分支而不是放宽到整表。
        if self.order_scope == "own" and not prefix:
            return StepResult(
                "cancel_all", symbol, False,
                detail={"refused": "no_own_namespace"},
                error=("cancel_all: 判不出本 bot 命名空间（label_prefix 与 label 均为空）"
                       "→ 拒绝整表撤单；请给 label 或配置 label_prefix"
                       "（确实要整表请显式 order_scope: all）"))
        use_own = self.order_scope == "own" and bool(prefix)
        if use_own:
            positions, perr = self._positions_or_error()
            if positions is None:
                return StepResult(
                    "cancel_all", symbol, False,
                    detail={"refused": "positions_unavailable"},
                    error=f"持仓查询失败，拒绝撤销挂单（无法区分保护单与孤儿）: {perr}",
                )
            # only this bot's open orders (text prefix), never wipe the book
            if symbol:
                self._check_symbol(symbol)
                symbols = [symbol]
            else:
                rows = self.client.list_orders() or []
                symbols = self._wide_symbols(scope, [o.get("contract") for o in rows])
            cancelled = []
            skipped = []
            errors = []
            for sym in symbols:
                owned = self._owned_open_orders(sym, prefix)
                pending_entry = self._has_pending_entry(sym)
                for o in owned:
                    oid = str(o.get("id") or "")
                    if not oid:
                        continue
                    if self._should_keep_protection(o, positions, pending_entry):
                        skipped.append(oid)
                        continue
                    try:
                        self.client.cancel_order(oid)
                        cancelled.append(oid)
                    except GateApiError as e:
                        errors.append(f"{oid}: {e}")
            ok = not errors
            return StepResult("cancel_all", symbol, ok, detail={
                "cancelled": cancelled, "skipped_live_protection": skipped,
                "errors": errors, "order_scope": "own", "prefix": prefix,
            }, error="; ".join(errors) if errors else None)
        # 非 own-scope：整表撤单无法逐单过滤 → 存在真实保护单时拒绝
        positions, perr = self._positions_or_error()
        if positions is None:
            return StepResult(
                "cancel_all", symbol, False,
                detail={"refused": "positions_unavailable"},
                error=f"持仓查询失败，拒绝撤销挂单（无法区分保护单与孤儿）: {perr}",
            )
        guarded = []
        for sym in ([symbol] if symbol else self._wide_symbols(scope, self._open_symbols())):
            if not sym:
                continue
            try:
                rows = self.client.list_orders(sym) or []
            except GateApiError:
                rows = []
            if self._live_protection_ids(rows, positions):
                guarded.append(sym)
        if guarded:
            return StepResult(
                "cancel_all", symbol, False,
                detail={"refused": "live_protection_present", "symbols": guarded},
                error=f"{guarded} 上有正在保护持仓的 reduce-only 挂单，整表撤单会撤掉它们，已拒绝",
            )
        if symbol:
            self._check_symbol(symbol)
            result = self.client.cancel_all_orders(symbol)
        else:
            # cancel open orders on every contract with open orders (not only open positions)
            orders = self.client.list_orders() or []
            symbols = self._wide_symbols(scope, [o.get("contract") for o in orders])
            result = {sym: self.client.cancel_all_orders(sym) for sym in symbols}
        return StepResult("cancel_all", symbol, True, detail={"result": result})

    def _positions_or_error(self) -> tuple[Optional[list], str]:
        """取 `/positions` 原始记录；**取数失败返回 `(None, 原因)`**。

        与 `_symbol_positions` 的区别：后者有意「取不到就当没有」（它服务清理路径，
        宁可漏清不可误清）。撤单场景里这个默认是**危险**的 —— 账户接口一抖就被读成
        「无持仓」，真实持仓的保护单会被当孤儿撤掉。所以必须把「取不到」和「取到空」分开。
        """
        try:
            return (self.client.get_positions() or []), ""
        except Exception as e:  # noqa: BLE001 — 任何异常都按「不可确认」处理
            return None, str(e)

    def _live_protection_ids(self, orders: list, positions: list) -> set:
        """从订单列表里挑出**正在保护真实持仓**的 reduce-only 单 id（撤单时必须跳过）。

        判据与 `_cleanup_orphan_protectors` 完全同源（`_order_is_reduce_only` +
        `_is_orphan_protector`），保证「自动清理」与「AI 直发的撤单」两条路径不漂移。
        """
        out = set()
        for o in orders or []:
            if not self._order_is_reduce_only(o):
                continue
            if self._is_orphan_protector(o, positions):
                continue  # 真孤儿 → 允许撤
            oid = self._order_id(o)
            if oid:
                out.add(str(oid))
        return out

    def _should_keep_protection(
        self, order: Optional[dict], positions: list, pending_entry: bool,
    ) -> bool:
        """撤单前：这一单是否**必须保留**（不能当陈旧单/孤儿撤掉）。

        判据与 `_cleanup_orphan_protectors` 同源（`_order_is_reduce_only` +
        `_is_orphan_protector`），避免「自动清理」与「AI 直发的撤单 / replace 撤旧单」
        三条路径漂移。两种保留理由：

        1. **有对应持仓** → 它正在保护真实持仓（撤了就是裸仓）
        2. **有待成交入场单** → 它可能是随入场单预挂的保护（委托一成交就是裸仓，
           线上实测 2026-10-02 就是这么裸的）
        """
        if not order or not self._order_is_reduce_only(order):
            return False
        if not self._is_orphan_protector(order, positions):
            return True
        return bool(pending_entry)

    def _price_order_symbols(self, symbol: str = "") -> list[str]:
        """本次 `cancel_price_all` 会触及的 symbol 列表（闸门与执行同源）。"""
        if symbol:
            return [symbol]
        return sorted({
            (p.get("contract") or (p.get("initial") or {}).get("contract") or "")
            for p in (self.client.list_price_orders(symbol) or [])
        } - {""})

    def _cancel_price_all(self, symbol: str, label: str = "", scope: str = "") -> StepResult:
        """撤销本 bot 的条件单（**带持仓感知**）。

        **为什么要带持仓感知**：AI 的意图通常是「撤孤儿保护单」（提示词规则 14），
        但它能用的动作是 `cancel_price_all` = 「撤我名下**全部**条件单」—— 一个**钝动作**。
        只要 AI 对持仓的判断错一次（实测 2026-10-04：账户接口抖动被读成 flat，
        AI 据此要撤 `t-wyk-tp`/`t-wyk-sl`，而 BTC_USDT 上 2366 张的真实持仓还在），
        钝动作就会连带撤掉**真实持仓的止损**。提示词的正确性不能作为安全前提，
        所以这里把钝动作收紧：**正在保护真实持仓的 tp/sl 一律跳过**，只撤入场类条件单。
        判据复用 `_cleanup_orphan_protectors` 那一套，避免两条路径漂移。

        持仓数据取不到时**整个拒绝** —— 无法区分保护单与孤儿，宁可不动。
        `symbol` 为空时必须显式 `scope`（原先静默放大到全账户）。
        """
        wide = not symbol
        if wide:
            scope = self._wide_scope(scope, "cancel_price_all")
        if symbol:
            self._check_symbol(symbol)
        positions, perr = self._positions_or_error()
        if positions is None:
            return StepResult(
                "cancel_price_all", symbol, False,
                detail={"refused": "positions_unavailable"},
                error=f"持仓查询失败，拒绝撤销条件单（无法区分保护单与孤儿）: {perr}",
            )
        prefix = self._own_prefix(label)
        # 判不出命名空间 → 拒绝（原先会静默退回整表撤单，见 `_cancel_all` 同处说明）
        if self.order_scope == "own" and not prefix:
            return StepResult(
                "cancel_price_all", symbol, False,
                detail={"refused": "no_own_namespace"},
                error=("cancel_price_all: 判不出本 bot 命名空间（label_prefix 与 label 均为空）"
                       "→ 拒绝整表撤单；请给 label 或配置 label_prefix"
                       "（确实要整表请显式 order_scope: all）"))
        use_own = self.order_scope == "own" and bool(prefix)
        # 宽作用面（symbol 为空）：候选面 = 账户上「有条件单的合约」，再按 scope 收窄
        syms = (self._price_order_symbols(symbol) if symbol
                else self._wide_symbols(scope, self._price_order_symbols("")))
        if use_own:
            cancelled = []
            skipped = []
            errors = []
            for sym in syms:
                owned = self._owned_price_orders(sym, prefix)
                pending_entry = self._has_pending_entry(sym)
                for p in owned:
                    pid = self._order_id(p)
                    if not pid:
                        continue
                    if self._should_keep_protection(p, positions, pending_entry):
                        skipped.append(pid)
                        continue
                    try:
                        self.client.cancel_price_order(pid)
                        cancelled.append(pid)
                    except GateApiError as e:
                        errors.append(f"{pid}: {e}")
            ok = not errors
            return StepResult("cancel_price_all", symbol, ok, detail={
                "cancelled": cancelled, "skipped_live_protection": skipped,
                "errors": errors, "order_scope": "own", "prefix": prefix,
            }, error="; ".join(errors) if errors else None)
        # 非 own-scope：走交易所整表撤单，**无法逐单过滤** → 存在真实保护单时直接拒绝
        guarded = []
        for sym in syms:
            try:
                rows = self.client.list_price_orders(sym) or []
            except GateApiError:
                rows = []
            if self._live_protection_ids(rows, positions):
                guarded.append(sym)
        if guarded:
            return StepResult(
                "cancel_price_all", symbol, False,
                detail={"refused": "live_protection_present", "symbols": guarded},
                error=f"{guarded} 上有正在保护持仓的 tp/sl，整表撤单会撤掉它们，已拒绝",
            )
        # **闸门与执行必须同源**：上面按 `syms`（已按 scope 收窄）查过保护单，这里也必须
        # 按 `syms` 逐合约撤。原先宽路径直接 `cancel_all_price_orders(None)` = 整账户，
        # 于是 `scope: bot` 只在闸门上生效、真正撤的仍是全账户 —— 收紧等于没做。
        if symbol:
            result = self.client.cancel_all_price_orders(symbol)
        else:
            result = {sym: self.client.cancel_all_price_orders(sym) for sym in syms}
        return StepResult("cancel_price_all", symbol, True, detail={"result": result})

    def _open_symbols(self) -> list[str]:
        positions = self.client.get_positions() or []
        symbols = []
        for p in positions:
            sym = p.get("contract")
            if sym and qty(p.get("size")) != 0 and sym not in symbols:
                symbols.append(sym)
        return symbols

    def _apply_order_type(self, body: dict, order_type: str, price: Optional[float], meta) -> None:
        if order_type == "market":
            body["price"] = "0"
            body["tif"] = "ioc"
            return
        if price is None:
            raise GateApiError(f"type={order_type} requires price")
        body["price"] = str(round_price(float(price), meta))
        body["tif"] = {
            "limit": "gtc",
            "post_only": "poc",
            "ioc": "ioc",
            "fok": "fok",
        }[order_type]

    def _place_trigger(
        self,
        intent: Intent,
        trigger_side: str,
        trigger_price: float,
        is_tp: bool,
        meta,
        size: float,
    ) -> dict:
        """Place a close price-trigger (TP/SL). trigger_side is the order side to close position."""
        if is_tp:
            rule = intent.trigger_rule_tp
            order_type = intent.tp_type or "market"
            limit = intent.tp_limit_price
        else:
            rule = intent.trigger_rule_sl
            order_type = intent.sl_type or "market"
            limit = intent.sl_limit_price

        # size for close-trigger: 默认用**显式张数**（历史行为）。
        #
        # `account_risk.use_venue_close: true` 时改用 **venue-computed close**：
        # `size=0` + `auto_size=close_long|close_short` 的语义是「平掉该方向全部
        # 仓位」—— 保护单**不需要跟踪持仓张数**，从根上消除「张数对齐」这个问题。
        #
        # ⚠️ **不能带 `close=true`**：实测 2026-10-27 真端点
        #   · `/price_orders` + `close` → `AUTO_INVALID_PARAM_CLOSE`
        #   · `auto_size` 必须配 `size=0 & reduce_only=true & close=false`
        #     （否则 `INVALID_ARGUMENT`）
        # 本方法原先同时写 `close=true` + `auto_size` —— 一旦打开开关必然 400，
        # 且一直默认关闭所以从未暴露。
        #
        # 小数张必须保留：`int(0.7)` = 0 → 挂出一条 0 张的「保护单」（等于裸仓）
        close_size = round(float(size), 8)
        # API: buy to close short (positive), sell to close long (negative)
        api_size = -close_size if (intent.side or "long") == "long" else close_size
        dual = False
        try:
            dual = str(self.client.get_position_mode() or "").lower() == "dual"
        except Exception:  # noqa: BLE001
            dual = False
        # 残仓（持仓不足 1 张，如 0.1 张幽灵腿）**只有** venue-close 能表达：
        # 触发单端点把 0.1 张向零取整成 0 → 闸门拒单（等于裸仓）。此时
        # 「全平该方向」恰好等于原意（本来就只剩 0.1 张），所以不依赖配置开关，
        # 张数 <1 就自动走 auto_size。**不能全局开 venue-close**：网格/阶梯 bot 的
        # 多档 TP 每档只平一部分，auto_size 会变成「第一档就平掉整侧」。
        fractional_residual = 0 < abs(close_size) < 1
        # 2026-10-28 实盘（pa-eth-v5）：持仓 13.8 张 = 0.138 ETH，SL/TP 被挂成
        # **−13 张**（显式整数）→ 触发时只平 13 张，残 0.8 张永久化。
        # 根因链：ETH `size_step=0.1`（enable_decimal）→ 入场 323U/(2340.5×0.01)
        # = 13.8 张可以下；但 `/price_orders` 的 `initial.size` 是 **int64**，
        # `_int_price_order_size` 向零取整 → 13。**小数张 + 显式张数保护 =
        # 必然残仓**，与「仓位大小」无关，只要带小数就会发生。
        #
        # 判据用「该保护单是否覆盖整个方向」，不是「是否有小数」：
        #   · 覆盖整仓（SL / 全仓 TP）：auto_size 语义完全等价 → 走 venue-close；
        #   · 部分档（网格/阶梯的多档 TP）：仍走显式张数，绝不能被放大成整侧平仓。
        abs_close = abs(close_size)
        has_fraction = abs(abs_close - math.floor(abs_close)) > 1e-9
        whole_position = False
        if has_fraction and not fractional_residual:
            try:
                qty = sum(abs(float(p.get("size") or 0))
                          for p in self._symbol_positions(intent.symbol)
                          if str(p.get("side") or "") == (intent.side or "long"))
                whole_position = qty > 0 and abs_close >= qty - 1e-9
            except Exception as e:  # noqa: BLE001 — 取不到持仓就退回显式张数
                swallow("executor._place_trigger.whole_position", e, level=logging.WARNING)
                whole_position = False
            if whole_position:
                log.warning(
                    "保护单张数 %.4f 带小数且覆盖整仓（持仓 %.4f）→ 走 auto_size"
                    "（price_orders 的 size 是 int64，显式张数会截断成残仓）",
                    abs_close, qty)
        use_venue_close = (bool((self.account_risk or {}).get("use_venue_close"))
                           or fractional_residual or whole_position)
        if use_venue_close and not dual:
            # 单向模式没有 `auto_size`；`close=true` 在触发单端点上未验证，
            # 不拿实盘裸奔 —— 退回显式张数（= 旧行为）。
            log.warning("use_venue_close 仅支持双向持仓模式；%s 为单向 → 退回显式张数",
                        intent.symbol)
            use_venue_close = False
        if use_venue_close:
            api_size = 0

        use_market = order_type == "market"
        if use_market:
            # Gate price-trigger: price=0 → market on trigger (guaranteed exit)
            init_price = "0"
            init_tif = "ioc"
        else:
            if limit is None:
                limit = default_trigger_limit_price(float(trigger_price), intent.side or "long", is_tp)
            init_price = str(round_price(float(limit), meta))
            init_tif = "gtc"

        init: dict[str, Any] = {
            "contract": intent.symbol,
            "size": api_size,
            "price": init_price,
            "tif": init_tif,
            "reduce_only": True,
            "text": f"t-{self._bot_tag(intent.label)}-{'tp' if is_tp else 'sl'}",
        }
        if use_venue_close:
            init["auto_size"] = ("close_long" if (intent.side or "long") == "long"
                                 else "close_short")

        body: dict[str, Any] = {
            "initial": init,
            "trigger": {
                "strategy_type": 0,
                "price_type": PRICE_TYPE_MAP.get(intent.trigger_price_type, 0),
                "price": str(round_price(float(trigger_price), meta)),
                "rule": int(rule),
            },
        }
        if intent.trigger_expiration and self.client.env == "live":
            body["trigger"]["expiration"] = int(intent.trigger_expiration)
        if intent.margin_mode:
            body["pos_margin_mode"] = intent.margin_mode

        # Gate price_orders body is nested {initial, trigger} (same as quick_order.cmd_trigger_order)
        return self.client.place_price_order(body)
