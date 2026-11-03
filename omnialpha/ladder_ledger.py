"""阶梯档位台账（2026-11-04）—— **全局系统层**，为所有策略/所有 bot 服务。

## 为什么要有这个模块

阶梯类策略（模板铺梯 / AI 逐腿 / 双向网格）都需要回答同一个问题：
**「哪一档已经成交、哪一档还挂着、这一档的止盈目标是多少、这轮还能再铺几档」**。
历史实现把这份状态分散在三处（`state/ladder_levels.json` 的裸 dict、每轮重算的
`want`、交易所挂单），三者没有唯一权威，于是踩了三个系统性坑（2026-11-03 核实）：

1. 🔴 **`order_id` 从不回写** —— 每轮 `_sync_template_levels` 把本侧 pending 档
   *全部删掉重建*，新行 `order_id=""`。台账与交易所**永远无法对账**：成交档识别
   不出来（永远 pending）、撤单也感知不到（死档只增不减）、止盈单 `tp_order_id`
   每轮清零重挂。
2. 🔴 **成交档被重建删除** —— 档位成交后，下一轮 `want` 可能因重锚/结构位漂移
   不再包含该价 → 那一行被当「旧 pending」删掉 → **持仓的 `tp_target` 凭空消失**
   → `ensure_grid_tp` 找不到来源 → 持仓无止盈（实盘裸奔 8 小时）。
3. 🔴 **层数预算不计已成交** —— 判定「梯子满没满」只看**在盘挂单数**，成交一档
   → 在盘 9 条 < 10 → 立刻在同一价补一条，于是**永远有 10 层未成交**，
   敞口随价格来回而无上限地重入。

本模块把这三件事收敛成一条规则：**台账 = 交易所的本地缓存，交易所是权威**。
每轮 `reconcile()` 以交易所为准纠正状态；`budget()` 以「已成交档数」扣减层数额度；
`upsert_pending()` 只追加真不存在的档（绝不整表重建）。

## 数据结构（`state/ladder_levels.json`）

```json
{"levels": [
  {"symbol":"ETH_USDT","side":"long","unit":"grid","price":2241.8,"size":1.0,
   "tp_target":2266.7,"tp_pct":1.11,"order_id":"...","tp_order_id":"",
   "entry_state":"open|filled","status":"pending|filled|protected|closed",
   "label":"t-mtgbi-grid","ts":"2026-11-04T00:00:00Z"}
]}
```

- `entry_state`：**入场单**状态（open / filled）—— 层数预算与成交判定只看它
- `status`：**生命周期**（pending 未挂 TP / protected 已挂 TP / filled 已成交待挂
  TP / closed 止盈落袋）—— 止盈维护看它
- 两个字段正交：入场成交（entry_state）与止盈挂没挂（status）互不等价

## 兼容

旧表没有 `entry_state` → `entry_state_of()` 按 `status` 推导（filled→filled，
其余 open）。不迁移、不破坏，读旧写新。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ST_PENDING = "pending"
ST_FILLED = "filled"
ST_PROTECTED = "protected"
ST_CLOSED = "closed"
#: 归档：该侧持仓归零后整代梯子的历史档。**不占层数额度**（新一代重新开始），
#: 但与 `closed` 分开 —— 否则平仓后旧的「已成交/已闭合」会永久吃掉预算。
ST_ARCHIVED = "archived"

#: 判定「同一档」的价格容差（相对值）。与 executor 的 `align_tol` 同量级：
#: 交易所 round 后的价格与模板价最多差一个 tick，0.02% 在 2200 上约 0.44 点。
DEFAULT_TOL = 0.0002


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read(path: Optional[Path]) -> dict:
    """读台账。文件不存在/损坏 → 空表（调用方只追加，不会因此丢数据）。"""
    try:
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                data.setdefault("levels", [])
                return data
    except Exception:  # noqa: BLE001 — 台账是增强，坏了重建
        pass
    return {"levels": []}


def write(path: Optional[Path], data: dict) -> None:
    """原子落盘（tmp + replace），失败不抛 —— 台账不拦主流程。"""
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        pass


def levels_of(data: dict) -> list:
    """返回**真身**列表（就地 append/remove 必须生效）。

    曾经这里写成列表推导返回副本 —— `upsert_pending` 往副本里 append，
    `data["levels"]` 一条也没加（实测「synced:10 / total:0」）。
    """
    rows = data.get("levels")
    if not isinstance(rows, list):
        rows = []
        data["levels"] = rows
    return rows


def entries(data: dict, symbol: str, side: Optional[str] = None) -> list[dict]:
    out = []
    for lv in levels_of(data):
        if not isinstance(lv, dict):
            continue
        if lv.get("symbol") != symbol:
            continue
        if side is not None and str(lv.get("side") or "long") != side:
            continue
        out.append(lv)
    return out


def entry_state_of(lv: dict) -> str:
    """入场单状态：新字段优先，旧行按 status 推导（filled/protected → 已成交）。"""
    st = str(lv.get("entry_state") or "").strip().lower()
    if st in ("open", "filled"):
        return st
    return "filled" if str(lv.get("status")) in (ST_FILLED,) else "open"


def pf(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def same_price(a: Any, b: Any, tol: float = DEFAULT_TOL) -> bool:
    fa, fb = pf(a), pf(b)
    if not fa or not fb:
        return False
    return abs(fa - fb) / fb <= tol


# ── 对账（交易所是权威）────────────────────────────────────────────────


def bind_by_price(data: dict, symbol: str, side: str, open_rows: Iterable[dict],
                  tol: float = DEFAULT_TOL) -> dict:
    """给**没有 order_id 的档**按价格回填交易所单号。

    历史表（`order_id` 全空）与新铺但未回写的档都靠这一步接上交易所 ——
    没有 order_id，`reconcile()` 永远判不出成交，第 1 条病根就修不掉。
    只在「该档还没成交」时回填：已成交档的入场单已不在盘，回填会张冠李戴。
    """
    rows = []
    for o in open_rows or []:
        if o.get("is_reduce_only") or o.get("reduce_only"):
            continue
        pid = str(o.get("id") or "")
        px = pf(o.get("price"))
        if pid and px:
            rows.append((pid, px, abs(float(o.get("size") or 0) or 0)))
    bound, missed = [], []
    for lv in entries(data, symbol, side):
        if str(lv.get("order_id") or ""):
            continue
        if entry_state_of(lv) == "filled":
            continue
        lpx = pf(lv.get("price"))
        if not lpx:
            continue
        best, best_d = None, 1e9
        for pid, px, sz in rows:
            d = abs(px - lpx) / lpx
            if d <= tol and d < best_d:
                best, best_d = pid, d
        if best:
            lv["order_id"] = best
            bound.append({"price": lpx, "order_id": best})
        else:
            missed.append(lpx)
    return {"bound": bound, "unbound": missed}


def reconcile(data: dict, symbol: str, side: str, open_ids: set,
              probe: Callable[[str], str], tol: float = DEFAULT_TOL) -> dict:
    """以交易所为准纠正入场单状态。`probe(order_id)` 返回 filled/gone/unknown。

    - 单号在盘 → `entry_state=open`
    - 单号不在盘且交易所说 filled → `entry_state=filled`（**保留该行**，它的
      `tp_target` 是持仓止盈的唯一来源）
    - 单号不在盘且已撤/查不到 → 删行（死档会污染 LLM 快照并让预算算错）
    - probe 抖动（unknown）→ **不动**（fail-closed，宁可不改也不误删）
    """
    filled, pruned, unknown = [], [], []
    levels = levels_of(data)
    for lv in entries(data, symbol, side):
        if entry_state_of(lv) == "filled":
            continue
        oid = str(lv.get("order_id") or "")
        if not oid:
            continue
        if oid in open_ids:
            lv["entry_state"] = "open"
            continue
        try:
            res = probe(oid)
        except Exception:  # noqa: BLE001 — 查询异常一律按 unknown
            unknown.append({"price": lv.get("price"), "order_id": oid})
            continue
        res = str(res or "unknown").lower()
        if res == "filled":
            lv["entry_state"] = "filled"
            if str(lv.get("status")) == ST_PENDING:
                lv["status"] = ST_FILLED
            lv.setdefault("filled_ts", now_ts())
            filled.append({"price": lv.get("price"), "order_id": oid})
        elif res == "gone":
            if lv in levels:
                levels.remove(lv)
            pruned.append({"price": lv.get("price"), "order_id": oid})
        else:
            unknown.append({"price": lv.get("price"), "order_id": oid})
    return {"filled": filled, "pruned": pruned, "unknown": unknown,
            "n_filled": len(filled), "n_pruned": len(pruned)}


def drop_phantoms(data: dict, symbol: str, side: str,
                  keep_prices: Iterable[Any], tol: float = DEFAULT_TOL) -> dict:
    """删掉「从没挂出去、也不在模板里」的幽灵 pending 档。

    它们来自历史轮次的 `want`（重锚/结构位漂移后已无对应价），既没有交易所单号
    也无对应挂单 —— 留着只会污染 LLM 快照与层数预算。**有 order_id 的档永不删**
    （那是真实挂单，去留由 `reconcile()` 的交易所对账决定）。
    """
    keep = [p for p in (pf(x) for x in keep_prices) if p]
    levels = levels_of(data)
    dropped = []
    for lv in list(entries(data, symbol, side)):
        if str(lv.get("order_id") or ""):
            continue
        if entry_state_of(lv) == "filled":
            continue
        if str(lv.get("status")) in (ST_PROTECTED, ST_CLOSED):
            continue
        lpx = pf(lv.get("price"))
        if not lpx:
            continue
        if not any(abs(lpx - k) / k <= tol for k in keep):
            if lv in levels:
                levels.remove(lv)
            dropped.append(lpx)
    return {"dropped": dropped}


def upsert_pending(data: dict, symbol: str, side: str, price: float, size: float,
                   unit: str, tp_target: Optional[float], tp_pct: Optional[float],
                   label: str = "", tol: float = DEFAULT_TOL) -> dict:
    """追加一档（已存在同价档则只补 `tp_target`，绝不重复建行）。"""
    px = pf(price)
    if not px:
        return {"skipped": "bad_price"}
    for lv in entries(data, symbol, side):
        if same_price(lv.get("price"), px, tol):
            # 释放过的档（止盈落袋 closed / 整代归档 archived）→ **重新开一代**：
            # 套利完成后额度已释放，这个价位应当能重新挂上入场单，否则台账会
            # 停在 closed、交易所上的新单永远绑不到这一行（等于又回到不可对账）。
            # 只按 status 判：`closed` = 该档止盈已成交（持仓已平），即便
            # `entry_state` 还是 filled 也要重开 —— 那正是「套利完成释放一档」。
            if str(lv.get("status")) in (ST_CLOSED, ST_ARCHIVED):
                lv["status"] = ST_PENDING
                lv["entry_state"] = "open"
                lv["order_id"] = ""
                lv.pop("tp_order_id", None)
                lv["reopened"] = int(lv.get("reopened") or 0) + 1
                lv["ts"] = now_ts()
                if tp_target:
                    lv["tp_target"] = round(float(tp_target), 4)
                if tp_pct:
                    lv["tp_pct"] = round(float(tp_pct), 4)
                if unit:
                    lv["unit"] = unit
                if size:
                    lv["size"] = float(size)
                return {"reopened": True, "price": px}
            if tp_target and not lv.get("tp_target"):
                lv["tp_target"] = float(tp_target)
            if not lv.get("unit"):
                lv["unit"] = unit
            return {"existed": True, "price": px}
    lv = {
        "symbol": symbol, "side": side, "unit": unit,
        "price": round(px, 4), "size": float(size),
        "tp_target": round(float(tp_target), 4) if tp_target else None,
        "tp_pct": round(float(tp_pct), 4) if tp_pct else None,
        "label": label, "order_id": "", "tp_order_id": "",
        "entry_state": "open", "status": ST_PENDING,
        "ts": now_ts(),
    }
    levels_of(data).append(lv)
    return {"added": True, "price": px}


def bind_order(data: dict, symbol: str, side: str, price: Any, order_id: str,
               tol: float = DEFAULT_TOL, create: bool = False,
               size: float = 0.0, unit: str = "", label: str = "") -> bool:
    """铺单成功后回写单号（第 1 条病根的正解）。

    `create=True` 时**行不存在就建行** —— 语义是「交易所上真有这张单，台账就必须有
    这一档」。不加这条会出现「单挂出去了但台账没记」（sync 按预算跳过新增时），
    下一轮 `bind_by_price` 也补不上，等于又回到不可对账。
    """
    oid = str(order_id or "")
    if not oid:
        return False
    for lv in entries(data, symbol, side):
        if same_price(lv.get("price"), price, tol):
            lv["order_id"] = oid
            if not lv.get("entry_state"):
                lv["entry_state"] = "open"
            if size and not lv.get("size"):
                lv["size"] = float(size)
            return True
    if not create:
        return False
    px = pf(price)
    if not px:
        return False
    levels_of(data).append({
        "symbol": symbol, "side": side, "unit": unit or "grid",
        "price": round(px, 4), "size": float(size or 0),
        "tp_target": None, "tp_pct": None, "label": label,
        "order_id": oid, "tp_order_id": "",
        "entry_state": "open", "status": ST_PENDING, "ts": now_ts(),
    })
    return True


def is_consumed(lv: dict) -> bool:
    """这一档是否**正在占用层数额度**（= 该档的持仓还开着）。

    语义（2026-11-04 用户口径「网格套利一次就释放一档」）：
    · `pending`（挂单在盘）/ `protected`(未成交) → **不占**（还没建仓）
    · `filled` / 已成交的 `protected`（持仓还在） → **占**（成交一档少一层）
    · `closed`（止盈落袋 = 套利完成） → **释放**，额度还回去，该档可重新开档
    · `archived`（整代归档） → 不占

    曾经把 `closed` 也算「已消耗」，结果是**套利一次层数就永久少一层**，
    梯子越跑越稀 —— 与网格的定义相反。
    """
    if str(lv.get("status")) in (ST_ARCHIVED, ST_CLOSED):
        return False
    return entry_state_of(lv) == "filled" or str(lv.get("status")) == ST_FILLED


def consumed_prices(data: dict, symbol: str, side: str) -> list:
    return [pf(lv.get("price")) for lv in entries(data, symbol, side)
            if is_consumed(lv) and pf(lv.get("price"))]


def budget(data: dict, symbol: str, side: str, total_slots: int) -> dict:
    """层数预算：**已成交档占额度**（第 3 条病根的正解）。

    「上限 10 层」= 已成交 k 档 + 可挂 10−k 档。成交一档后不再补满 10 条未成交，
    新档只能占用剩下的额度，且价格从**下一层**开始（由调用方按最新价格铺）。
    """
    try:
        total = int(total_slots)
    except (TypeError, ValueError):
        total = 0
    filled = [lv for lv in entries(data, symbol, side) if is_consumed(lv)]
    live = [lv for lv in entries(data, symbol, side)
            if not is_consumed(lv)
            and str(lv.get("status")) in (ST_PENDING, ST_PROTECTED)]
    remaining = total - len(filled) if total > 0 else 0
    released = [lv for lv in entries(data, symbol, side)
                if str(lv.get("status")) == ST_CLOSED]
    return {
        "filled": len(filled),
        "filled_prices": consumed_prices(data, symbol, side),
        "pending": len(live),
        "released": len(released),
        "total": total,
        "remaining": max(0, remaining),
    }


def _total_key(symbol: str, side: str) -> str:
    return f"{symbol}|{side}"


def set_total(data: dict, symbol: str, side: str, total: int) -> int:
    """记录「这一代梯子一共几层」—— 预算的上限基准。

    为什么不用配置里的 `grid_levels`：AI 允许在 floor 之上要更多层（实测
    `two_way.levels=12` → 实际铺 12 层）。上限若写死 10，会把 AI 主动加深的梯子
    砍回 10 层（`test_ai_more_than_floor_kept` 锁的既有语义）。正确口径是
    **「一代梯子首次铺出来的层数」**：成交 k 档后剩 N−k 档，N 就是那一代的总数。
    """
    try:
        n = int(total)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    totals = data.get("totals")
    if not isinstance(totals, dict):
        totals = {}
        data["totals"] = totals
    totals[_total_key(symbol, side)] = n
    return n


def get_total(data: dict, symbol: str, side: str) -> int:
    totals = data.get("totals")
    if not isinstance(totals, dict):
        return 0
    try:
        return int(totals.get(_total_key(symbol, side)) or 0)
    except (TypeError, ValueError):
        return 0


def clear_total(data: dict, symbol: str, side: str) -> None:
    totals = data.get("totals")
    if isinstance(totals, dict):
        totals.pop(_total_key(symbol, side), None)


def archive_flat(data: dict, symbol: str, side: str) -> int:
    """该侧已无持仓 → 把已成交档归档为 closed，预算回满（下一轮重新从 10 层开始）。"""
    n = 0
    for lv in entries(data, symbol, side):
        if entry_state_of(lv) == "filled":
            lv["entry_state"] = "open"
            lv["status"] = ST_ARCHIVED
            lv["archived_ts"] = now_ts()
            n += 1
    if n:
        clear_total(data, symbol, side)   # 新一代梯子重新定层数
    return n


def mark_filled(data: dict, symbol: str, side: str, price: Any,
                tol: float = DEFAULT_TOL) -> bool:
    """把某档标为已成交（人工/策略侧确认时用；日常由 `reconcile()` 自动判定）。"""
    for lv in entries(data, symbol, side):
        if same_price(lv.get("price"), price, tol):
            lv["entry_state"] = "filled"
            if str(lv.get("status")) == ST_PENDING:
                lv["status"] = ST_FILLED
            lv.setdefault("filled_ts", now_ts())
            return True
    return False


#: 同一价位多行时保留优先级：持仓中的 > 已挂 TP 的 > 待成交的 > 已闭合/归档
_STATUS_PRI = {ST_FILLED: 4, ST_PROTECTED: 3, ST_PENDING: 2, ST_CLOSED: 1,
               ST_ARCHIVED: 0}


def dedupe(data: dict, symbol: str, side: str, tol: float = DEFAULT_TOL) -> dict:
    """同一价位只留一行（2026-11-04 部署实测：super-grid 出现了同一价 3 行）。

    来源：`_record_ladder_level`（AI 逐腿）曾**盲目 append**，与模板 sync 写下的
    那一行并存 → 同价 filled/closed/pending 三行，预算与止盈都会算错。
    合并规则：保留优先级最高的一行，把其余行的 `order_id`/`tp_target` 补进来。
    """
    rows = entries(data, symbol, side)
    groups: list[list[dict]] = []
    for lv in rows:
        for g in groups:
            if same_price(g[0].get("price"), lv.get("price"), tol):
                g.append(lv)
                break
        else:
            groups.append([lv])
    removed = []
    bag = levels_of(data)
    for g in groups:
        if len(g) < 2:
            continue
        g.sort(key=lambda x: (_STATUS_PRI.get(str(x.get("status")), -1),
                              bool(x.get("order_id"))), reverse=True)
        keep = g[0]
        for drop in g[1:]:
            for k in ("order_id", "tp_order_id", "tp_target", "tp_pct", "size"):
                if not keep.get(k) and drop.get(k):
                    keep[k] = drop.get(k)
            if drop in bag:
                bag.remove(drop)
            removed.append(drop.get("price"))
    return {"removed": len(removed), "prices": removed}


# ── 一次性播种：从持仓反推已成交档（2026-11-04 部署迁移用）──────────────
def seed_key(symbol: str, side: str) -> str:
    return f"{symbol}|{side}"


def is_seeded(data: dict, symbol: str, side: str) -> bool:
    seeded = data.get("seeded")
    return isinstance(seeded, dict) and bool(seeded.get(seed_key(symbol, side)))


def mark_seeded(data: dict, symbol: str, side: str, info: dict = None) -> None:
    seeded = data.get("seeded")
    if not isinstance(seeded, dict):
        seeded = {}
        data["seeded"] = seeded
    seeded[seed_key(symbol, side)] = info or {"ts": now_ts()}


def seed_from_position(data: dict, symbol: str, side: str, want: list,
                       tps: dict, pos: float, tol: float = DEFAULT_TOL,
                       entry: float = 0.0) -> dict:
    """用**当前持仓**反推「这一侧已经成交了几档」，把对应的档标成 filled。

    为什么需要（2026-11-04 首次部署本台账时）：服务器上的旧表 `order_id` 全空，
    成交档已经不在交易所挂单里 —— 新台账**认不出**哪些档已经成交。若直接按
    「零消耗」起算，价格回来时会把已成交的档当成空闲额度重新挂一遍 → 敞口
    超过层数上限（10 层 + 已有持仓）。

    做法：按 `want` 的**由近到远**顺序累加张数，凑够持仓张数即视为这几档已成交
    （阶梯本来就从近端开始成交）。一次性 —— 播种过就不再猜（之后全靠
    `reconcile()` 的单号对账，那才是真凭据）。

    只读 `want` 里的档，不动交易所；取不到持仓时调用方不会走到这里（fail-closed）。
    """
    try:
        need = float(pos)
    except (TypeError, ValueError):
        return {"skipped": "bad_position"}
    if need <= 0:
        mark_seeded(data, symbol, side, {"ts": now_ts(), "filled": 0})
        return {"skipped": "no_position"}
    dist_g = float((tps or {}).get("grid", 0) or 0)
    if entry > 0:
        # 🔴 有**真实入场价**就用它建一行（持仓 = 一档聚合），不要猜「最近模板档」：
        # 实测 super-grid 持仓入场 2279.31，却把最近模板档 2320.49 标成已成交 →
        # 由它派生的 tp(≈2298) 对空单是**亏损价**（空单止盈必须在入场下方），
        # `ensure_short_tp` 一旦补覆盖就会挂出亏损止盈。
        tp = entry * (1 + dist_g / 100.0) if side == "long"             else entry * (1 - dist_g / 100.0)
        row = None
        for lv in entries(data, symbol, side):
            if same_price(lv.get("price"), entry, tol):
                row = lv
                break
        if row is None:
            row = {
                "symbol": symbol, "side": side, "unit": "grid",
                "price": round(float(entry), 4), "size": need,
                "tp_target": round(tp, 4), "tp_pct": round(dist_g, 4),
                "label": "", "order_id": "", "tp_order_id": "",
                "ts": now_ts(),
            }
            levels_of(data).append(row)
        row["entry_state"] = "filled"
        row["status"] = ST_FILLED
        row["seeded"] = True
        row.setdefault("filled_ts", now_ts())
        mark_seeded(data, symbol, side,
                    {"ts": now_ts(), "filled": 1, "position": need,
                     "entry": float(entry), "src": "entry_price"})
        return {"seeded": 1, "lots": need, "position": need, "entry": float(entry),
                "levels": [float(entry)]}
    filled, acc = [], 0.0
    for (px, lots), unit in want:
        try:
            sz = float(lots)
            p = float(px)
        except (TypeError, ValueError):
            continue
        if sz <= 0 or p <= 0:
            continue
        dist = (tps or {}).get(unit, (tps or {}).get("grid", 0.0))
        tp = p * (1 + dist / 100.0) if side == "long" else p * (1 - dist / 100.0)
        row = None
        for lv in entries(data, symbol, side):
            if same_price(lv.get("price"), p, tol):
                row = lv
                break
        if row is None:
            row = {
                "symbol": symbol, "side": side, "unit": unit,
                "price": round(p, 4), "size": sz,
                "tp_target": round(tp, 4), "tp_pct": round(dist, 4),
                "label": "", "order_id": "", "tp_order_id": "",
                "ts": now_ts(),
            }
            levels_of(data).append(row)
        row["entry_state"] = "filled"
        row["status"] = ST_FILLED          # 已成交、尚未挂 TP → ensure_*_tp 会接手
        row["seeded"] = True
        row.setdefault("filled_ts", now_ts())
        filled.append({"price": p, "size": sz})
        acc += sz
        if acc >= need - 1e-9:
            break
    mark_seeded(data, symbol, side,
                {"ts": now_ts(), "filled": len(filled), "position": need})
    return {"seeded": len(filled), "lots": acc, "position": need,
            "levels": [f["price"] for f in filled]}
