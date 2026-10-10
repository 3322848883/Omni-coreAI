#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性回填：把**历史** ledger 行的 symbol / symbols_json 从已有落盘产物推回来。

审计 D-15 的收尾。T15（e852a92）修的是**新写入**（`tradelog.log_execution` →
`insert_trade(symbols=...)`、`ledger._add_missing_columns` 补列），而
`migrate.py` 有 `.jsonl_imported` 标记不会重导 —— 于是历史行至今是 NULL，
`recent_trades(symbol=…)` 筛不到旧数据（按币复盘历史失效；不影响交易）。

数据源（字段都从代码核实过，不猜）：
  `logs/trades/<bot>.jsonl`（旧布局）+ `data/bots/<bot>/logs/trades.jsonl`（v2）
      每行 `steps[i]["symbol"]` —— 即 `executor.py:72` 的 `ExecReport.to_dict()`
      写在**步骤顶层**的 symbol。`detail` / `signal_meta` 里没有 symbol。
  `archive/done/<bot>/*.result.json`（旧）+ `data/bots/<bot>/archive/done/*.result.json`
      **同一份** `to_dict()` 落盘（watcher.py:460，仅 `report.ok` 时写）。
  `plans.raw_json` 里的 `chips[].symbol`：migrate 导入的历史 plan 行把整行 JSON
      塞进了 raw_json（migrate.py:89），所以它自带 symbol。
  `plans` 的 jsonl plan 行：`type=plan`，symbol 取 `chips[].symbol`（migrate.py:87 同口径）。

匹配键（`trades` 表**没有** order_id 列；`order_ids` 只被 insert_trade 写成 `[]`，
从来没人传过值 —— 用不了，别照着这个名字去 join）：
  主键 `(bot_id, steps 摘要)`：库里的 `steps_json` 与 jsonl 里的 `steps` 是同一份
      payload 的两次序列化，归一化（键排序 + 紧凑分隔符）后可比。比 plan_cycle 强：
      同一轮可以执行多次、persona 信号压根没有 plan_cycle。
  兜底 `(bot_id, plan_cycle)`：只在**两侧都恰好剩 1 条**时配对 —— 宁可漏补，
      也不张冠李戴（猜错就是把别的币写到了这一笔上）。
  `plans` 按 `(bot_id, cycle_id)`，同样要求两侧唯一。

只 UPDATE `trades.symbol` / `trades.symbols_json` / `plans.symbols_json` 三个列，
且每条 SQL 自带 `IS NULL` 守卫（只补空，已有值一个都不改）。不删行、不改表结构。

用法::

    python scripts/backfill_symbols.py                       # dry-run：统计 + 样例
    python scripts/backfill_symbols.py --bot brooks-btc      # 只看一个 bot
    python scripts/backfill_symbols.py --apply               # 真正写库
    python scripts/backfill_symbols.py --root /opt/omnialpha --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omnialpha.ledger import Ledger, default_ledger_path, symbols_from_steps  # noqa: E402

TRADES = "trades"
PLANS = "plans"
SAMPLE_LIMIT = 3


# --------------------------------------------------------------------- 来源解析

def _norm_key(steps: Any) -> str:
    """`steps` 的**归一化**摘要（排序键 + 紧凑分隔符）。

    同一份 payload 在库里是 `steps_json` 文本、在 jsonl 里是原文，两次序列化的
    键序/空白可能不同 —— 归一化后才是可比的同一个键。
    """
    txt = json.dumps(steps if steps is not None else [], ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"), default=str)
    return sha1(txt.encode("utf-8")).hexdigest()


def _digest_of_json(txt: Any) -> str:
    """库里 `steps_json` 文本 → 与 `_norm_key` 同口径的摘要；解析不了给空串（退回兜底键）。"""
    if not txt:
        return ""
    try:
        return _norm_key(json.loads(txt))
    except Exception:  # noqa: BLE001
        return ""


def _loads(txt: Any) -> dict:
    if not txt:
        return {}
    try:
        v = json.loads(txt)
    except Exception:  # noqa: BLE001
        return {}
    return v if isinstance(v, dict) else {}


def _plan_symbols_of(row: dict) -> list[str]:
    """plan 行的 symbol：`chips[].symbol` 优先，其次行顶层的 `symbols` / `symbol`。

    与 `migrate.py:87` 同口径 —— 只在行里**显式声明过**的字段上取值，不猜。
    """
    chips = row.get("chips")
    raw: list[Any] = []
    if isinstance(chips, list):
        raw += [c.get("symbol") if isinstance(c, dict) else c for c in chips]
    if not raw:
        return Ledger._symbols_of(row)
    return Ledger._symbols_of({"symbols": raw})


def _to_ts(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Cand:
    """一条**可回填候选** —— 从落盘产物里解析出来的「某笔执行用了哪些币」。"""
    bot_id: str
    table: str          # trades / plans
    symbols: list[str]
    key: str            # trades: steps 摘要；plans: cycle_id
    cycle: str          # 兜底配对用（plan_cycle / cycle_id）
    ts: float
    source: str
    status: str = ""    # 匹配阶段回填：fill / has_value / dup / ambiguous / unmatched
    method: str = ""    # 命中的方法：steps / plan_cycle / chips / raw_json


def _src(srcs: dict[str, dict], path: str, kind: str) -> dict:
    e = srcs.get(path)
    if e is None:
        e = srcs[path] = {
            "path": path, "kind": kind, "lines": 0, "bad_lines": 0, "candidates": 0,
            "matched": 0, "to_update": 0, "skipped_has_value": 0,
            "skipped_duplicate": 0, "skipped_ambiguous": 0, "skipped_unmatched": 0,
            "skipped_no_symbol": 0, "skipped_other": 0,
        }
    return e


def _bots_of(root: Path, bots: list[str]) -> list[str]:
    if bots:
        return list(dict.fromkeys(str(b) for b in bots))
    base = root / "data" / "bots"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def _dedupe(pairs: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """同一个文件可能同时被旧布局与 v2 路径扫到（--jsonl-dir 指到 v2 目录时）。"""
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for p, hint in pairs:
        try:
            rp = str(Path(p).resolve())
        except OSError:
            rp = str(p)
        if rp in seen:
            continue
        seen.add(rp)
        out.append((Path(p), hint))
    return out


def _jsonl_files(root: Path, jsonl_dir: Path, bots: list[str]) -> list[tuple[Path, str]]:
    """(文件, bot 提示)。两套布局都扫 —— migrate 搬走旧文件后只剩 v2，反过来也一样。"""
    pairs: list[tuple[Path, str]] = []
    if jsonl_dir.is_dir():
        pairs += [(p, p.stem) for p in sorted(jsonl_dir.glob("*.jsonl"))]
    for bot in _bots_of(root, bots):
        p = root / "data" / "bots" / bot / "logs" / "trades.jsonl"
        if p.is_file():
            pairs.append((p, bot))
    return _dedupe(pairs)


def _result_files(root: Path, archive_dir: Path, bots: list[str]) -> list[tuple[Path, str]]:
    """(result.json, bot 提示)。旧布局是 `<archive>/<bot>/*.result.json`，
    v2 是 `data/bots/<bot>/archive/done/*.result.json`（migrate 把前者搬成后者）。"""
    pairs: list[tuple[Path, str]] = []
    if archive_dir.is_dir():
        for bot_dir in sorted(p for p in archive_dir.iterdir() if p.is_dir()):
            pairs += [(p, bot_dir.name) for p in sorted(bot_dir.glob("*.result.json"))]
    for bot in _bots_of(root, bots):
        d = root / "data" / "bots" / bot / "archive" / "done"
        if d.is_dir():
            pairs += [(p, bot) for p in sorted(d.glob("*.result.json"))]
    return _dedupe(pairs)


def _iter_jsonl(path: Path, st: dict):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        st["lines"] += 1
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            st["bad_lines"] += 1
            continue
        if isinstance(row, dict):
            yield row
        else:
            st["bad_lines"] += 1


def collect_candidates(root: Path, jsonl_dir: Path, archive_dir: Path,
                       bots: list[str]) -> tuple[list[Cand], dict[str, dict]]:
    """扫落盘产物 → (候选, 每来源统计)。坏文件/坏行只跳过，不让整次回填失败。"""
    botset = set(bots or [])
    cands: list[Cand] = []
    srcs: dict[str, dict] = {}

    for path, hint in _jsonl_files(root, jsonl_dir, bots):
        st = _src(srcs, str(path), "jsonl")
        for row in _iter_jsonl(path, st):
            bot_id = str(row.get("bot_id") or hint or "").strip()
            if botset and bot_id not in botset:
                st["skipped_other"] += 1
                continue
            kind = str(row.get("type") or ("execution" if row.get("steps") is not None else ""))
            if kind == "execution":
                syms = symbols_from_steps(row.get("steps"))
                if not syms:
                    st["skipped_no_symbol"] += 1
                    continue
                st["candidates"] += 1
                cands.append(Cand(bot_id, TRADES, syms, _norm_key(row.get("steps")),
                                  str(row.get("plan_cycle") or ""), _to_ts(row.get("ts")),
                                  str(path), method="steps"))
            elif kind == "plan":
                syms = _plan_symbols_of(row)
                if not syms:
                    st["skipped_no_symbol"] += 1
                    continue
                cyc = str(row.get("plan_cycle") or row.get("cycle_id") or "").strip()
                st["candidates"] += 1
                cands.append(Cand(bot_id, PLANS, syms, cyc, cyc, _to_ts(row.get("ts")),
                                  str(path), method="chips"))
            else:
                st["skipped_other"] += 1

    for path, hint in _result_files(root, archive_dir, bots):
        st = _src(srcs, str(path), "result.json")
        st["lines"] += 1
        if botset and hint not in botset:
            st["skipped_other"] += 1
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            st["bad_lines"] += 1
            continue
        if not isinstance(data, dict):
            st["bad_lines"] += 1
            continue
        syms = symbols_from_steps(data.get("steps"))
        if not syms:
            st["skipped_no_symbol"] += 1
            continue
        st["candidates"] += 1
        cands.append(Cand(hint, TRADES, syms, _norm_key(data.get("steps")), "", 0.0,
                          str(path), method="steps"))

    return cands, srcs


# --------------------------------------------------------------------- 写库与匹配

class Updater:
    """攒待写的 UPDATE，最后**一个短事务**落库。

    每条 SQL 自带 `IS NULL` 守卫：只有「还是空的」才写 —— 即使读到的快照与库里的
    现状不一致（别的进程刚补过），也不会覆盖已有的值。这就是幂等的最后一层保险。
    """

    def __init__(self) -> None:
        self.batches: dict[str, list[tuple]] = {}

    def set_symbol(self, row_id: int, value: str) -> None:
        self._add("UPDATE trades SET symbol=? WHERE id=? AND symbol IS NULL", (value, row_id))

    def set_symbols_json(self, table: str, row_id: int, value: str) -> None:
        self._add(f"UPDATE {table} SET symbols_json=? WHERE id=? AND symbols_json IS NULL",
                  (value, row_id))

    def _add(self, sql: str, params: tuple) -> None:
        self.batches.setdefault(sql, []).append(params)

    def apply(self, conn: sqlite3.Connection) -> int:
        n = 0
        with conn:  # 短事务：全部 UPDATE 一次提交
            for sql, params in self.batches.items():
                cur = conn.executemany(sql, params)
                n += max(cur.rowcount, 0)
        return n


class _Ctx:
    def __init__(self, conn: sqlite3.Connection, cols: dict[str, set[str]],
                 srcs: dict[str, dict], bots: list[str]):
        self.conn = conn
        self.cols = cols
        self.srcs = srcs
        self.botset = set(bots or [])
        self.updates = Updater()
        self.samples: list[dict] = []

    def src(self, path: str) -> dict:
        return _src(self.srcs, path, "db")

    def sample(self, table: str, row_id: int, bot_id: str, source: str,
               old: dict, new: dict) -> None:
        if len(self.samples) < SAMPLE_LIMIT:
            self.samples.append({"table": table, "id": row_id, "bot_id": bot_id,
                                 "source": source, "old": old, "new": new})


def _connect(db_path: Path, read_only: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if read_only:
        # dry-run 的「不写库」交给 SQLite 自己保证：任何写操作直接报错，
        # 比「我们的代码里没有 UPDATE」更硬。
        conn.execute("PRAGMA query_only=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _fill_trade(ctx: _Ctx, row: dict, cand: Cand, have_symbol: bool, have_json: bool,
                stat: dict, method: str) -> bool:
    syms = cand.symbols
    symbol_val = ",".join(syms)  # 与 insert_trade 同口径：单币=该币，多币=逗号串
    sjs_val = json.dumps(syms, ensure_ascii=False)
    cols: list[str] = []
    if have_symbol and row["symbol"] is None:
        ctx.updates.set_symbol(row["id"], symbol_val)
        stat["fills"]["symbol"] += 1
        cols.append("symbol")
    if have_json and row["symbols_json"] is None:
        ctx.updates.set_symbols_json(TRADES, row["id"], sjs_val)
        stat["fills"]["symbols_json"] += 1
        cols.append("symbols_json")
    if not cols:
        cand.status = "has_value"
        return False
    cand.status = "fill"
    cand.method = method
    stat["by_method"][method] = stat["by_method"].get(method, 0) + 1
    ctx.sample(TRADES, row["id"], row["bot_id"], cand.source,
               {"symbol": row["symbol"], "symbols_json": row["symbols_json"]},
               {"symbol": symbol_val, "symbols_json": sjs_val})
    return True


def _fill_plan(ctx: _Ctx, row: dict, syms: list[str], cand: Optional[Cand],
               stat: dict, method: str, source: str) -> bool:
    if not syms:
        return False
    sjs_val = json.dumps(syms, ensure_ascii=False)
    ctx.updates.set_symbols_json(PLANS, row["id"], sjs_val)
    stat["fills"]["symbols_json"] += 1
    stat["by_method"][method] = stat["by_method"].get(method, 0) + 1
    ctx.sample(PLANS, row["id"], row["bot_id"], source,
               {"symbols_json": row["symbols_json"]}, {"symbols_json": sjs_val})
    if cand is not None:
        cand.status = "fill"
        cand.method = method
    else:
        # 库内自给（raw_json）：没有对应的来源候选行，统计当场结清 —— 否则报告会
        # 出现「匹配=0 却更新了 1 行」这种自相矛盾的数字。
        st = ctx.src(source)
        st["matched"] += 1
        st["to_update"] += 1
        stat["matched"] += 1
        stat["to_update"] += 1
    return True


def _match_trades(ctx: _Ctx, cands: list[Cand]) -> dict:
    cols = ctx.cols[TRADES]
    have_symbol, have_json = "symbol" in cols, "symbols_json" in cols
    sel = ["id", "bot_id", "plan_cycle", "steps_json"]
    sel.append("symbol" if have_symbol else "NULL AS symbol")
    sel.append("symbols_json" if have_json else "NULL AS symbols_json")
    rows: list[dict] = []
    for r in ctx.conn.execute(f"SELECT {', '.join(sel)} FROM trades ORDER BY id"):
        d = dict(r)
        if ctx.botset and d["bot_id"] not in ctx.botset:
            continue
        d["digest"] = _digest_of_json(d.get("steps_json"))
        d["needs"] = (have_symbol and d["symbol"] is None) or (have_json and d["symbols_json"] is None)
        d["matched"] = False
        rows.append(d)

    stat = {
        "rows": len(rows),
        "null_symbol": sum(1 for r in rows if r["symbol"] is None),
        "null_symbols_json": sum(1 for r in rows if r["symbols_json"] is None),
        "matched": 0, "to_update": 0, "skipped_has_value": 0, "db_unmatched": 0,
        "fills": {"symbol": 0, "symbols_json": 0}, "by_method": {},
    }
    if not (have_symbol or have_json):
        # 只补空、不改表结构 —— 缺列就报出来让人去跑一次 Ledger 建列。
        stat["note"] = "trades 表既没有 symbol 也没有 symbols_json 列（不改表结构，跳过）"
        return stat

    by_digest: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r["digest"]:
            by_digest.setdefault((r["bot_id"], r["digest"]), []).append(r)

    tcs = [c for c in cands if c.table == TRADES]
    seen: set[tuple[str, str]] = set()
    for c in sorted(tcs, key=lambda x: (x.ts, x.source)):
        k = (c.bot_id, c.key)
        bucket = by_digest.get(k)
        if bucket:
            row = bucket.pop(0)  # 一进一出：同一 payload 重复出现时按时间顺序配对
            seen.add(k)
            row["matched"] = True
            if row["needs"]:
                _fill_trade(ctx, row, c, have_symbol, have_json, stat, "steps")
            else:
                c.status = "has_value"
            continue
        if k in seen:
            c.status = "dup"  # 同一 payload 的副本（jsonl + result.json 各一份）
        else:
            seen.add(k)
            c.status = "unmatched"

    _cycle_fallback_trades(ctx, tcs, rows, have_symbol, have_json, stat)

    for c in tcs:
        st = ctx.src(c.source)
        if c.status == "fill":
            st["matched"] += 1
            st["to_update"] += 1
            stat["matched"] += 1
            stat["to_update"] += 1
        elif c.status == "has_value":
            st["matched"] += 1
            st["skipped_has_value"] += 1
            stat["matched"] += 1
            stat["skipped_has_value"] += 1
        elif c.status == "dup":
            st["skipped_duplicate"] += 1
        elif c.status == "ambiguous":
            st["skipped_ambiguous"] += 1
        else:
            st["skipped_unmatched"] += 1
    stat["db_unmatched"] = sum(1 for r in rows if r["needs"] and not r["matched"])
    return stat


def _cycle_fallback_trades(ctx: _Ctx, tcs: list[Cand], rows: list[dict],
                           have_symbol: bool, have_json: bool, stat: dict) -> None:
    """`(bot_id, plan_cycle)` 唯一配对 —— **只在两侧都恰好剩 1 条**时用。

    steps 摘要是强键；这条兜底是给「库里的 steps_json 与 jsonl 对不上」留的窄缝
    （空 steps、旧版序列化）。唯一性约束是为了宁可漏补也不张冠李戴：同一轮里可能
    有好几笔执行，猜错就等于把 A 币写到了 B 笔上。
    """
    left_c: dict[tuple[str, str], list[Cand]] = {}
    for c in tcs:
        if c.status == "unmatched" and c.cycle:
            left_c.setdefault((c.bot_id, c.cycle), []).append(c)
    left_r: dict[tuple[str, str], list[dict]] = {}
    done_r: dict[tuple[str, str], int] = {}
    for r in rows:
        if not r.get("plan_cycle"):
            continue
        k = (r["bot_id"], str(r["plan_cycle"]))
        if r["needs"] and not r["matched"]:
            left_r.setdefault(k, []).append(r)
        else:
            done_r[k] = done_r.get(k, 0) + 1  # 已有值，或本轮刚被补上

    for k, cs in left_c.items():
        rs = left_r.get(k) or []
        if len(rs) == 1 and len(cs) == 1:
            _fill_trade(ctx, rs[0], cs[0], have_symbol, have_json, stat, "plan_cycle")
            rs[0]["matched"] = True
        elif rs:
            for c in cs:
                c.status = "ambiguous"  # 有候选也有空行，但不止一个 —— 不猜
        elif done_r.get(k):
            for c in cs:
                c.status = "has_value"  # 那一轮的行已经有币了（或刚被 steps 摘要补上）


def _match_plans(ctx: _Ctx, cands: list[Cand]) -> dict:
    cols = ctx.cols[PLANS]
    have_json = "symbols_json" in cols
    sel = ["id", "bot_id", "cycle_id", "raw_json"]
    sel.append("symbols_json" if have_json else "NULL AS symbols_json")
    rows: list[dict] = []
    for r in ctx.conn.execute(f"SELECT {', '.join(sel)} FROM plans ORDER BY id"):
        d = dict(r)
        if ctx.botset and d["bot_id"] not in ctx.botset:
            continue
        d["needs"] = d["symbols_json"] is None
        d["matched"] = False
        rows.append(d)

    stat = {
        "rows": len(rows),
        "null_symbols_json": sum(1 for r in rows if r["symbols_json"] is None),
        "matched": 0, "to_update": 0, "skipped_has_value": 0, "db_unmatched": 0,
        "fills": {"symbols_json": 0}, "by_method": {},
    }
    if not have_json:
        stat["note"] = "plans 表没有 symbols_json 列（不改表结构，跳过）"
        return stat

    # (0) raw_json 自带 chips：行自己的 payload，不需要跟谁配对
    for r in rows:
        if not r["needs"]:
            continue
        syms = _plan_symbols_of(_loads(r.get("raw_json")))
        if syms and _fill_plan(ctx, r, syms, None, stat, "raw_json", "db:plans.raw_json"):
            r["matched"] = True

    # (1) jsonl 的 plan 行：按 (bot_id, cycle_id) 唯一配对（同 trades 兜底的理由）
    pcs = sorted((c for c in cands if c.table == PLANS), key=lambda x: (x.ts, x.source))
    left_c: dict[tuple[str, str], list[Cand]] = {}
    for c in pcs:
        if c.cycle:
            left_c.setdefault((c.bot_id, c.cycle), []).append(c)
        else:
            c.status = "unmatched"
    left_r: dict[tuple[str, str], list[dict]] = {}
    done_r: dict[tuple[str, str], int] = {}
    for r in rows:
        if not r.get("cycle_id"):
            continue
        k = (r["bot_id"], str(r["cycle_id"]))
        if r["needs"] and not r["matched"]:
            left_r.setdefault(k, []).append(r)
        else:
            done_r[k] = done_r.get(k, 0) + 1  # 已有值，或本轮刚被补上

    for k, cs in left_c.items():
        rs = left_r.get(k) or []
        if len(rs) == 1 and len(cs) == 1:
            if _fill_plan(ctx, rs[0], cs[0].symbols, cs[0], stat, "chips", cs[0].source):
                rs[0]["matched"] = True
        elif rs:
            for c in cs:
                c.status = "ambiguous"
        elif done_r.get(k):
            for c in cs:
                c.status = "has_value"
        else:
            for c in cs:
                c.status = "unmatched"

    for c in pcs:
        st = ctx.src(c.source)
        if c.status == "fill":
            st["matched"] += 1
            st["to_update"] += 1
            stat["matched"] += 1
            stat["to_update"] += 1
        elif c.status == "has_value":
            st["matched"] += 1
            st["skipped_has_value"] += 1
            stat["matched"] += 1
            stat["skipped_has_value"] += 1
        elif c.status == "dup":
            st["skipped_duplicate"] += 1
        elif c.status == "ambiguous":
            st["skipped_ambiguous"] += 1
        else:
            st["skipped_unmatched"] += 1
    stat["db_unmatched"] = sum(1 for r in rows if r["needs"] and not r["matched"])
    return stat


def backfill(db_path: Path, cands: list[Cand], *, apply: bool = False,
             srcs: Optional[dict[str, dict]] = None,
             bots: Optional[list[str]] = None) -> dict:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(str(db_path))
    conn = _connect(db_path, read_only=not apply)
    try:
        ctx = _Ctx(conn, {t: _columns(conn, t) for t in (TRADES, PLANS)},
                   srcs if srcs is not None else {}, bots or [])
        trades = _match_trades(ctx, cands)
        plans = _match_plans(ctx, cands)
        res = {
            "db": str(db_path),
            "apply": bool(apply),
            "candidates": len(cands),
            "sources": sorted(ctx.srcs.values(), key=lambda e: e["path"]),
            "tables": {TRADES: trades, PLANS: plans},
            "planned_rows": trades["to_update"] + plans["to_update"],
            "samples": ctx.samples,
        }
        # 写库放在最后：dry-run 时这条路径根本不执行（连接还带 PRAGMA query_only）
        res["applied"] = ctx.updates.apply(conn) if apply else 0
        return res
    finally:
        conn.close()


def run(root: Path, *, db: Optional[Path] = None, jsonl_dir: Optional[Path] = None,
        archive_dir: Optional[Path] = None, bots: Optional[list[str]] = None,
        apply: bool = False) -> dict:
    root = Path(root).resolve()
    cands, srcs = collect_candidates(
        root,
        Path(jsonl_dir) if jsonl_dir else root / "logs" / "trades",
        Path(archive_dir) if archive_dir else root / "archive" / "done",
        list(bots or []),
    )
    return backfill(Path(db) if db else default_ledger_path(root), cands,
                    apply=apply, srcs=srcs, bots=list(bots or []))


# ------------------------------------------------------------------------ 输出

def _fmt(res: dict) -> str:
    L: list[str] = []
    A = L.append
    A("=" * 78)
    A("回填 ledger symbol / symbols_json（审计 D-15 的历史行收尾）")
    A(f"库: {res['db']}")
    A(f"模式: {'APPLY（已写库）' if res['apply'] else 'DRY-RUN（只统计，不写库）'}")
    A("-" * 78)
    A("来源:")
    if not res["sources"]:
        A("  (没有任何来源文件 —— logs/trades/*.jsonl 与 archive/done/<bot>/*.result.json 都不存在)")
    for s in res["sources"]:
        tail = "   (库内自给，没有对应的来源文件)" if s["kind"] == "db" else ""
        A(f"  {s['path']}{tail}")
        A(f"      行={s['lines']} 坏行={s['bad_lines']} 候选(带 symbol)={s['candidates']}"
          f"  匹配={s['matched']} 将更新={s['to_update']}")
        A(f"      跳过: 库行已有值={s['skipped_has_value']} 重复={s['skipped_duplicate']}"
          f" 不唯一={s['skipped_ambiguous']} 匹配不上={s['skipped_unmatched']}"
          f" 来源无 symbol={s['skipped_no_symbol']} 非执行/别的 bot={s['skipped_other']}")
    A("-" * 78)
    A("库:")
    for t in (TRADES, PLANS):
        s = res["tables"][t]
        A(f"  {t}: 行={s['rows']}  symbol IS NULL={s.get('null_symbol', '-')}"
          f"  symbols_json IS NULL={s['null_symbols_json']}")
        A(f"      匹配来源={s['matched']} → 将更新 {s['to_update']} 行"
          f"（symbol 列={s['fills'].get('symbol', 0)}"
          f" / symbols_json 列={s['fills'].get('symbols_json', 0)}）"
          f"  匹配方式={s['by_method'] or '{}'}")
        A(f"      库内无来源={s['db_unmatched']} 行  来源指向的行已有值={s['skipped_has_value']}")
        if s.get("note"):
            A(f"      ! {s['note']}")
    if res["apply"]:
        A(f"  实际写入：SQL 影响行数={res['applied']}")
    A("-" * 78)
    A("样例（旧值 → 新值）:")
    if not res["samples"]:
        A("  (无 —— 没有需要补的行)")
    for x in res["samples"]:
        A(f"  {x['table']}#{x['id']}  bot={x['bot_id']}  来源={x['source']}")
        for col in ("symbol", "symbols_json"):
            if col in x["new"]:
                A(f"      {col}: {x['old'].get(col)!r} → {x['new'][col]!r}")
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="回填历史 ledger 行的 symbol / symbols_json（默认 dry-run，--apply 才写库）")
    ap.add_argument("--root", default=str(ROOT), help="仓库根（默认=脚本所在仓库）")
    ap.add_argument("--db", default="", help="ledger 库（默认 <root>/data/bots.db）")
    ap.add_argument("--jsonl-dir", default="", help="成交日志目录（默认 <root>/logs/trades）")
    ap.add_argument("--archive-dir", default="", help="归档目录（默认 <root>/archive/done）")
    ap.add_argument("--bot", action="append", default=[], help="只处理这个 bot，可重复")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只统计与预览）")
    args = ap.parse_args(argv)

    try:
        res = run(Path(args.root),
                  db=Path(args.db) if args.db else None,
                  jsonl_dir=Path(args.jsonl_dir) if args.jsonl_dir else None,
                  archive_dir=Path(args.archive_dir) if args.archive_dir else None,
                  bots=args.bot, apply=args.apply)
    except FileNotFoundError as e:
        print(f"! 库不存在: {e}（用 --db 指定）", file=sys.stderr)
        return 1
    print(_fmt(res))
    if not args.apply and res["planned_rows"]:
        print()
        print(f"(dry-run: 以上 {res['planned_rows']} 行**没有**写入；确认无误后加 --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
