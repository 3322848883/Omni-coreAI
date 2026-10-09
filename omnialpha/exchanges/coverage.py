"""per-venue 覆盖矩阵：这个币在**这个交易所**到底有没有。

## 为什么需要单独一层

「工具返回空」有两种完全不同的原因，而模型与运维都分不出来：

1. **上币差异** —— 该所就没上这个合约（如 binance 无 PEPE、bitget 无 TON）。
   这不是数据故障，换所或用别的币就行。
2. **采集/接口故障** —— 该所有这个合约但这次没取到。这是**要修的问题**。

把两者混在一起看，前者会被当成故障去排查（白费时间），后者会被当成"正常的上币
差异"放过（真的故障被忽略）。这一层只回答「有没有」，不回答「取到没有」——
后者是 `aux_monitor` / `data_monitor` 的事。

## 为什么是三态而不是布尔

本仓只有 Gate 接了「列合约」的校验（`config.fetch_exchange_contracts`）。其余所要
用真实接口逐个探合约是否存在，而**本机网络到不了其中几家**（bitget TLS 重置等）。
与其把 AGENTS.md 里的上币差异抄成硬编码静态表（上币随时会变，抄下来就是**会腐坏的
知识**），不如如实回答 `None` = 未校验：`True`/`False` 是**测出来的**，`None` 是
**不知道**。调用方据此决定要不要去探。
"""
from __future__ import annotations

from typing import Optional

# 各所「列合约」能力的接入状态。True = 有实时校验路径；False = 尚未接入。
# 新增一家所时在这里改一行，而不是在业务代码里加分支。
_VENUE_LISTING_SUPPORT = {
    "gate": True,          # `config.fetch_exchange_contracts` 已接
    "binance": False,
    "okx": False,
    "bybit": False,
    "bitget": False,
    "hyperliquid": False,
}


def venues() -> list[str]:
    return list(_VENUE_LISTING_SUPPORT)


def supports_listing(exchange: str) -> bool:
    """该所是否已接入「列合约」校验。"""
    return bool(_VENUE_LISTING_SUPPORT.get(str(exchange or "").strip().lower(), False))


def coverage_matrix(symbols, *, venue_symbols: Optional[dict] = None) -> dict:
    """`{symbol: {venue: True | False | None}}`。

    - `True`  —— 该所**确认**有这个合约（调用方提供了实测集合）
    - `False` —— 该所**确认**没有（上币差异或拼错）
    - `None`  —— **未校验**（该所没接列合约能力，或这次没拿到）

    `venue_symbols` 是调用方手上**已有的实测集合** `{venue: {symbol, ...}}` ——
    本层不自己发请求（它要能在离线/无凭据下工作），只做机械比对。
    """
    out: dict = {}
    given = {str(k).strip().lower(): {str(s).strip().upper()
                                     for s in (v or [])}
             for k, v in (venue_symbols or {}).items()}
    for sym in (symbols or []):
        key = str(sym or "").strip().upper()
        if not key:
            continue
        row: dict = {}
        for v in venues():
            known = given.get(v)
            if not known:
                row[v] = None
            else:
                row[v] = key in known
        out[key] = row
    return out


def gaps(matrix: dict) -> dict:
    """从矩阵里挑出**确认没有**的格子 → `{symbol: [venue, ...]}`（上币差异清单）。"""
    out: dict = {}
    for sym, row in (matrix or {}).items():
        miss = sorted(v for v, ok in (row or {}).items() if ok is False)
        if miss:
            out[sym] = miss
    return out


def unverified(matrix: dict) -> dict:
    """**未校验**（`None`）的格子 → `{symbol: [venue, ...]}`：别把「不知道」当好或坏。"""
    out: dict = {}
    for sym, row in (matrix or {}).items():
        unk = sorted(v for v, ok in (row or {}).items() if ok is None)
        if unk:
            out[sym] = unk
    return out


def _main(argv) -> int:
    """`python -m omnialpha.exchanges.coverage BTC_USDT [gate=BTC_USDT,SOL_USDT ...]`。

    位置参数是要查的币；`venue=sym1,sym2` 形式的参数是**你手上的实测集合**
    （该所的合约清单）——提供了就出 `有/没有`，没提供就是 `?`（未校验）。
    加上这个入口是因为：只把矩阵留在库里、验收却只靠单测，等于它在生产里
    从没被用过（`?` 与 `有/没有` 的区别也就没人真的看见）。
    """
    argv = list(argv or [])
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        print("\n用法: python -m omnialpha.exchanges.coverage BTC_USDT [gate=BTC_USDT,SOL_USDT ...]")
        return 0 if argv else 2
    symbols: list = []
    venue_symbols: dict = {}
    for a in argv:
        if "=" in a:
            v, _, rest = a.partition("=")
            venue_symbols[v.strip().lower()] = [s.strip() for s in rest.split(",") if s.strip()]
        else:
            symbols.append(a)
    m = coverage_matrix(symbols, venue_symbols=venue_symbols)
    mark = {True: "有", False: "没有", None: "?"}
    for sym, row in m.items():
        cells = "  ".join(f"{v}={mark.get(ok, '?')}" for v, ok in sorted(row.items()))
        print(f"{sym}: {cells}")
    g = gaps(m)
    if g:
        print("\n确认没有（上币差异，换所或换币就行）：")
        for sym, vs in g.items():
            print(f"  {sym}: {', '.join(vs)}")
    u = unverified(m)
    if u:
        print("\n未校验（该所没接列合约能力；别把 ? 当有或没有）：")
        for sym, vs in u.items():
            print(f"  {sym}: {', '.join(vs)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_main(sys.argv[1:]))
