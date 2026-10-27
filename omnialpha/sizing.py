"""Convert strategy size_usd into Gate futures contract counts."""

from __future__ import annotations

import logging
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional

from .gate_client import ContractMeta, GateApiError

log = logging.getLogger(__name__)


# ── 小数张：唯一精度口径 ─────────────────────────────────────
# 2026-10-28 实盘：ETH 13.8 张（0.138 ETH）在 float 下可能变成
# 13.799999999999999，交易所按步长 floor 后成 **13.7** —— 每笔少 0.1 张，
# 且这种误差在「仓位 ↔ 保护单」对齐时会被放大成残仓。统一用 Decimal，
# 并按步长的小数位输出，禁止用 float 运算后再 round。

def _dec(x) -> Decimal:
    """float/str/int → Decimal，并把 float 的二进制噪声压到 1e-9。

    典型输入：`13.799999999999999`（上游 float 乘除的产物）—— 直接按 0.1 步长
    floor 会掉成 **13.7**（每笔少 0.1 张）。压到 1e-9 后还原成 13.8。
    """
    return Decimal(str(x)).quantize(Decimal("1e-9"))


def step_decimals(step: float) -> int:
    """步长需要几位小数：0.1→1、0.01→2、1→0、10→0。"""
    d = _dec(step).normalize()
    exp = d.as_tuple().exponent
    return max(0, -int(exp)) if isinstance(exp, int) else 0


def floor_to_step(qty: float, step: float) -> Decimal:
    """按步长向下取整（Decimal，无二进制误差）。"""
    q, s = _dec(qty), _dec(step)
    if s <= 0:
        s = Decimal(1)
    if q <= 0:
        return Decimal(0)
    return (q / s).to_integral_value(rounding=ROUND_FLOOR) * s


def round_to_step(qty: float, step: float) -> Decimal:
    """按步长四舍五入（Decimal，替代银行家舍入的 round()）。"""
    q, s = _dec(qty), _dec(step)
    if s <= 0:
        s = Decimal(1)
    if q <= 0:
        return Decimal(0)
    return (q / s).to_integral_value(rounding=ROUND_HALF_UP) * s


def qty_to_wire(qty: float, step: float) -> float | int:
    """把张数格式化成可以直接上 wire 的值。

    - 步长 ≥1（BTC/DOGE 这类整张合约）→ 返回 `int`，避免 13.0 被部分客户端拒
    - 步长带小数（ETH/SOL/PEPE…）→ 返回**已是步长整数倍**的 float（13.8 而不是
      13.799999999999999），交易所侧不会再 floor 掉一位
    """
    v = floor_to_step(qty, step)
    if step_decimals(step) == 0:
        return int(v)
    return float(v)


def vol_adjust_size(base_size_usd: float, atr_pct: float,
                    target_pct: float = 2.0,
                    clamp_lo: float = 0.5, clamp_hi: float = 2.0) -> float:
    """ATR 目标波动缩放仓位。波动大时减仓，波动小时加仓。

    base_size_usd: 基准仓位
    atr_pct: 当前 ATR 百分比（如 2.0 = 2%）
    target_pct: 目标波动百分比（0=禁用）
    clamp_lo/hi: 缩放倍数上下限，防极端值
    """
    if not base_size_usd or base_size_usd <= 0:
        return base_size_usd or 0
    if not target_pct or target_pct <= 0:
        return base_size_usd
    if not atr_pct or atr_pct <= 0:
        return base_size_usd
    ratio = target_pct / atr_pct
    ratio = max(clamp_lo, min(clamp_hi, ratio))
    return round(base_size_usd * ratio, 2)


def pct_to_size_usd(pct: float, base_usdt: float) -> float:
    """Convert asset ratio to notional size_usd. pct is 0..1 (0.1 = 10%)."""
    if pct is None or pct <= 0 or pct > 1:
        raise GateApiError(f"pct must be in (0, 1], got {pct!r}")
    if base_usdt is None or base_usdt <= 0:
        raise GateApiError(f"base_usdt must be positive, got {base_usdt!r}")
    return float(base_usdt) * float(pct)


def usd_to_contracts(
    size_usd: float,
    entry_price: float,
    meta: ContractMeta,
) -> float:
    """contracts = size_usd / (price * quanto_multiplier)，按**交易所步长**向下取整。

    返回 **float**：小数张合约（ETH `order_size_min=0.1`）下整数无法表达 0.7 张。
    """
    if size_usd is None or size_usd <= 0:
        raise GateApiError("size_usd must be positive")
    if entry_price is None or entry_price <= 0:
        raise GateApiError("entry_price must be positive")
    # `quanto_multiplier` 缺失 → **拒绝**，不再静默兜底 1.0。
    # 拿 1.0 顶替会让「张数 ↔ 名义金额」差几个数量级（spec S2.3 #5），而 paper 路径
    # 对这种输入是**硬拒**的 —— 同一份元数据两条路径语义必须同源，否则 live 会悄悄
    # 下一笔大 10000× 的单，而 paper 上同样的输入直接报错（回测结论因此不可信）。
    multiplier = meta.quanto_multiplier
    if multiplier is None or float(multiplier) <= 0:
        raise GateApiError(
            f"合约 {getattr(meta, 'name', '')!r} 的 quanto_multiplier 缺失或非正"
            f"（{multiplier!r}）→ 拒绝换算张数；请确认该所的合约元数据接口可用"
        )
    multiplier = float(multiplier)
    # 除法也走 Decimal：`100 / (0.00001 × 1e7)` 在 float 里会是
    # 0.9999999999999999 → floor 成 0.9 张（PEPE 这类 quanto 极大的币会踩到）。
    raw = _dec(size_usd) / (_dec(entry_price) * _dec(multiplier))
    # 步长**必须**取交易所真实值（`size_step`：enable_decimal → 0.1，否则 1）。
    # 原来用 `meta.order_size_round` —— 交易所根本不提供这个字段（1061 个合约
    # 0 命中），恒为假值 1 → ETH 的 0.1 步长对代码不可见，永远只能下整数张。
    step = safe_step(meta)
    # Decimal 取整：`math.floor(raw/step)*step` 在 13.8/0.1 这类除法上会出
    # 12.9999999 的尾巴，交易所再 floor 就少一张（每笔 0.1 张的静默缩水）。
    contracts = float(floor_to_step(raw, step))
    size_min = float(getattr(meta, "size_min", 0) or 0) or step
    if contracts < size_min:
        # **兜底：抬到「最小可下量」，不抛错**。
        # `size_usd` 是模型按「风险预算 ÷ 止损距离」反推的，小账户配远止损时
        # 会算出小于最小可下量的名义（实测 16.15 < 25.76）—— 抛错会让**整轮计划
        # 作废**并回滚已挂的腿。风险闸门是 yaml 风控 + 账户级上限，不靠这一条拦。
        # ⚠️ 抬到 `size_min` 而**不是 1 张**：ETH 上抬到 1 张 = 23.25U，
        # 而模型原本只想要 ~16U（+41%）；抬到 0.1 张 = 2.3U 才是真「最小」。
        log.warning(
            "size_usd=%.4f 不足最小可下量（价 %.2f × 乘数 %.6g → %.4f 张 < %.4f），已抬到 %.4f 张",
            size_usd, entry_price, multiplier, raw, size_min, size_min,
        )
        return round(size_min, 8)
    size_max = float(getattr(meta, "size_max", 0) or 0)
    if size_max > 0 and contracts > size_max:
        contracts = math.floor(size_max / step + 1e-9) * step
        log.warning("size_usd=%.4f 超过 size_max=%.4g，已夹到 %.4f 张",
                    size_usd, size_max, contracts)
        return round(contracts, 8)
    return contracts


def default_trigger_limit_price(
    trigger_price: float,
    side: str,
    is_tp: bool,
    slip_ratio: float = 0.001,
) -> float:
    """Mirror quick_order guidance: sell a bit below trigger, buy a bit above."""
    if trigger_price <= 0:
        raise GateApiError("trigger_price must be positive")
    slip = trigger_price * slip_ratio
    if is_tp:
        # take profit close: sell (long) near trigger → slightly below
        return trigger_price - slip if side == "long" else trigger_price + slip
    # stop loss close: aggressive exit
    return trigger_price - slip if side == "long" else trigger_price + slip


def round_price(price: float, meta: ContractMeta) -> float:
    step = meta.order_price_round or 0
    if step <= 0:
        return price
    precision = max(0, int(round(-math.log10(step))) if step < 1 else 0)
    rounded = round(price / step) * step
    return float(f"{rounded:.{precision}f}")


def safe_step(meta: ContractMeta) -> float:
    """张数步长（唯一来源）。`ContractMeta.size_step` 已封装交易所口径，这里只做兜底。"""
    try:
        step = float(getattr(meta, "size_step", 0) or 0)
    except (TypeError, ValueError):
        step = 0.0
    if step <= 0:
        step = 1.0
    return step


def quantize_size(qty: float, meta: ContractMeta, *, mode: str = "floor",
                  at_least_step: bool = True) -> float:
    """把张数对齐到交易所步长，并夹到 `[size_min, size_max]`。

    - `mode="floor"`：向下取整（默认，**保证不超过请求的名义/风险**）
    - `mode="round"`：四舍五入（份数表重算张数时用，贴近设计份数）
    - `at_least_step=True`：小于一个步长会抬到 `size_min`（= 最小可下量）

    **不能用内置 `round`**：Python 是银行家舍入（`round(42.5)==42` 但 `round(43.5)==44`），
    同一份数表在不同价格下会忽上忽下；这里统一 `floor(x + 0.5)`。
    """
    step = safe_step(meta)
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        q = 0.0
    if q <= 0:
        q = 0.0
    # Decimal 取整：float 版 `math.floor(q/step)*step` 会带出 12.999999 这类
    # 尾巴（13.8/0.1 在 float 里不精确），下游再 floor 一次就少一张。
    n = float(round_to_step(q, step) if mode == "round" else floor_to_step(q, step))

    size_min = float(getattr(meta, "size_min", 0) or 0) or step
    if at_least_step and n < size_min:
        n = size_min
    size_max = float(getattr(meta, "size_max", 0) or 0)
    if size_max > 0 and n > size_max:
        n = float(floor_to_step(size_max, step))
    return n
