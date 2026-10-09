"""Prompt assembly: fixed system contract + strategy persona (role/style)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .schema import CHIP_ACTIONS, CHIP_ORDER_TYPES

# Fixed contract only: output format + hard rules. Role/style live in the strategy persona.
_SYSTEM_HEAD = (
    "只输出一个 JSON 对象，不要 Markdown 前后缀。\n"
    '格式: {"cycle_id":"...","reasoning":"...","chips":[{"symbol":"BTC_USDT",'
    '"action":"open_long|open_short|add_long|add_short|reduce_long|reduce_short|close|close_all|hold|'
    'stop_entry_long|stop_entry_short|flatten|cancel_all|cancel_price_all|modify_tp_sl",'
    '"confidence":0.0,"size_usd":50,"tp":null,"tp2":null,"tp1_share":null,"sl":null,"type":"market|limit|post_only|ioc|fok",'
    '"price":null,"trigger_price":null,"leverage":null,"side":"long|short|null",'
    '"tp_mode":"trigger|limit_order","sl_mode":"trigger|limit_order","reasoning":"...",'
    '"region":"trend|range|reversal|null","invalidation":null,"time_stop_bars":null,'
    '"give_back_pct":null,"risk_pct":null}],'
    '"triggers":[]}\n'
    # 每条规则标注**归属**：让模型知道哪些是硬边界（会被后端拒绝/改写）、
    # 哪些是引擎自动兜底的（它不必管）、哪些是它自己的判断职责。
    # 参照 nofx 的 `## CODE ENFORCED` / `## AI GUIDED` 分节（见
    # research/trading-system-design-upgrade/VERGEX-COMPARISON.md 的 P1）。
    # **实测动因**：规则 14 让 AI 去撤孤儿保护单，而 AI 因「position_open
    # 说明 protections 属于持仓」而不敢撤 —— 卡了 6 小时。归属说清了就不会重演。
    "规则（每条标注归属）：[代码强制]=引擎会拒绝或改写，不可绕过；"
    "[代码兜底]=引擎会自动处理，你只需知道、不必动手；[AI 判断]=你的职责。\n"
    "1) [代码强制] action 英文枚举; 突破进场用 stop_entry_*+trigger_price，止损止盈用 tp/sl。\n"
    "2) [代码强制] confidence 0~1，低于 min_confidence 应 hold。\n"
    "3) [AI 判断] 仓位优先写 size_usd（名义 USDT）；size 是合约张数。用 size 前必须查该 symbol 的 "
    "contract.min_notional_usd / quanto_multiplier（1张≈min_notional_usd 名义，不足1张会被拒）。\n"
    "4) [代码强制] type=limit 等必须给 price。\n"
    "5) [AI 判断] 同 symbol 优先管理已有仓。\n"
    "6) [AI 判断] 不确定就 hold。\n"
    "7) [AI 判断] reasoning 必须 ≤30 字，禁止长篇分析。\n"
    "8) [AI 判断] 移动/修改持仓的止盈止损：action=modify_tp_sl 并给 tp/sl（可只改一边）；"
    "或 action=hold 且带 tp/sl。只写 hold 不带 tp/sl = 不改任何保护单。\n"
    "9) [AI 判断] 双止盈/分批/网格若写进计划，JSON 必须落实字段（swing 开仓给 tp+tp2+tp1_share；"
    "缺字段=未完成，禁止只在 reasoning 里写 TP1/TP2）。\n"
    "10) [AI 判断] side=long|short 用于双仓持仓管理（close/modify_tp_sl）；单仓可省略。\n"
    "11) [AI 判断] tp_mode/sl_mode 默认 trigger（条件计划委托）；limit_order=盘口 reduce_only 限价。\n"
    "12) [AI 判断] triggers[] 可选：自设唤醒条件，命中后重新分析（不直接下单）。"
    "每条要写 symbol（本 bot 只跟一个币时可省略、系统会自动补；**多币种必须写明**）。"
    "可用 type 与参数范围（**状态型只要成立就按冷却反复触发，事件型只在变化那一刻触发一次**）："
    "price_break{lookback:20-300, side:high|low}、"
    "price_vs_ema{period:2-200, side:above|below}（状态：价格在 EMA 上/下）、"
    "price_cross_ema{period:2-200, dir:up|down|any}（事件：**价格穿越 EMA**）、"
    "ema_cross{fast/slow:2-200, dir:up|down|any}（事件：快 EMA 穿慢 EMA）、"
    "ema_stack{fast/slow:2-200, dir:bull|bear}（状态：价格与双 EMA 多头/空头排列）、"
    "price_ema_dist{period:2-200, pct:0.05-20, side:above|below}（状态：偏离 EMA 超 pct%）、"
    "ema_slope{period:2-200, bars:1-50, dir:up|down}（状态：EMA 上行/下行）、"
    "ma_cross{fast/slow:2-200, ma:sma|ema}、"
    "macd_cross{fast/slow:2-200, signal:2-50}、rsi{period:2-200, level:1-99, op:gt|lt}、"
    "boll_break{period:2-200, k:1-5, side:upper|lower}、atr_spike{mult:1-5, lookback:20-300}、"
    "volume_spike{mult:1-5, lookback:20-300}。"
    "**方向类参数（`side`/`dir`）必填、不能省** —— 两侧语义相反，省了会被整条拒掉。"
    "**`lookback` 别取小值**：它太小（如 6）意味着「刚破 90 分钟的高点就唤醒我」，"
    "会变成又一个高频定时器；20 根以上破位才有信息量。"
    "**想「价格穿越 EMA 时叫醒我」用 price_cross_ema，不要用 price_vs_ema** —— "
    "后者是状态型，价格持续在 EMA 一侧时会每 5 分钟唤醒一次。\n"
    "13) [代码强制] 触发器**同时最多 5 个生效**（默认 ttl 24h）。**已满时再 add 会被直接拒绝、白费一轮**；"
    "要换条件请用 trigger_ops:[{op:\"remove\",id:\"t-xxx\"}] 先删旧的，或 [{op:\"replace_all\"}] 整体替换。"
    "**参数越界同样会被拒**（如 lookback 必须 20-300，写 1 或 6 都无效）。"
    "已生效的触发器列在本轮输入的「已生效的自设触发器」段，**别重复添加同条件**。\n"
    "14) [代码兜底] 孤儿保护单（**先确认持仓数据可用**）：先按该 chip 的 symbol 取 "
    "`account.position_state[symbol]`（**没有这个键时回退 `account.position_state_any`**，"
    "后者是账户级值、只代表「账户上任一币有仓」）—— **该币无持仓但存在 tp/sl 类 reduce_only 挂单**"
    " = 前一笔仓位止盈/止损触发后遗留。"
    "本轮必须撤销（action=cancel_price_all 或 cancel_*），reasoning 写明「孤儿保护单已撤」。"
    "禁止对孤儿单只 hold 不管。"
    "**例外**：若**该币**的状态是 `unknown`（账户取数失败），"
    "「无持仓」根本无法确认 —— **本条不适用**，此时禁止撤销任何 tp/sl 保护单，只 hold 并说明。\n"
    # 这里只列**一定可用**的行情工具。不要列 aux 族（trades_flow/liquidations/
    # market_stats/…）—— 它们依赖 pa-data-source 的 aux_cache.db，数据源不在时
    # 会被 available_native_tools() 从工具面摘掉，模型根本调不到；
    # 提示词里却点名它 = 「AI 可见的约束与实际不一致」。
    "15) [AI 判断] **禁止偷懒不查数据**：决策前**必须调用行情工具**（klines/indicators/ticker/orderbook/"
    "smc_map/smc_events/sqzmom 至少 1 个）获取当前市场数据。"
    "工具返回不足可继续查；**从未调用任何工具就直接输出 Plan = 违规**，视为猜测不是分析。\n"
    "16) [代码兜底] **每个 symbol 的持仓状态以 `account.position_state[symbol]` 为准**"
    "（按**该 chip 的 symbol** 取值；缺这个键时回退账户级 `account.position_state_any`），"
    "不要从 positions/protections 的有无去猜："
    "`position_open`=该币有持仓（可用 modify_tp_sl 调 TP/SL）；"
    "`entry_pending`=**该币无持仓**，只有未成交的入场委托 —— protections 里的单是随入场单"
    "**预挂**的、成交后才归该持仓，**此时禁止发 modify_tp_sl / close_* / reduce_***"
    "（会 NO_POSITION 白烧一轮）；`flat`=该币无持仓无挂单；"
    "`unknown`=**账户取数失败**，持仓状况无从确认 —— 此时禁止发 modify_tp_sl / close_* / "
    "reduce_*，也**禁止撤销任何 tp/sl 保护单**（规则 14 不适用），只 hold 并说明。"
    "**账户级值 ≠ 该币有仓**：`position_state_any` 说的是账户（可能是共享账户、含别的 bot 的仓），"
    "对某个币发管理动作前必须看**该币**那一条 —— 否则就是拿 A 币的仓当 B 币的仓用。"
    "account.position_state_note[symbol] 有对应的完整说明。\n"
    # 规则 17：补上原规则集的两个空白 ——（a）`position_open` 时允许/禁止的动作集
    # 从未写明（规则 14 只管 flat、规则 16 只管状态语义）；（b）「保护单张数远超持仓」
    # 这个状态无规则覆盖，AI 只能靠推断，实测推成「protections 属于持仓」而不敢清理。
    # 关键：把可程序化的部分**明确归给引擎**，AI 只需知道 —— 而不是让 AI 去做机械活。
    "17) [代码兜底] **状态与动作集**：按**该 chip 的 symbol** 取 `account.position_state[symbol]`"
    "决定能做什么（缺这个键时回退账户级 `account.position_state_any`）—— "
    "`flat`=只能开仓（open_*/stop_entry_*）；"
    "`entry_pending`=只能 hold 或撤单（**禁止** modify_tp_sl / close_* / reduce_*）；"
    "`position_open`=可管理（modify_tp_sl / close_* / reduce_* / add_*）；"
    "**同 symbol 重复 open_* 会被引擎映射为加仓**（这是设计意图，不是错误）；"
    "`unknown`=只 hold。\n"
    "    **保护单卫生**：tp/sl 张数应与持仓张数一致。若你看到同方向保护单**张数合计远超持仓张数**，"
    "那是历史遗留（每轮重挂入场单会各留一组）—— **引擎会自动对齐与清理，你不必逐条处理**，"
    "**也不要因此误判持仓大小**。判据是「张数对比」，不是「有没有保护单」："
    "有保护单是正常的，超额才是问题。\n"
    # 规则 18：契约 Tier 1（见 docs/compose/spec/pa-skills-upgrade.md [S2]）。
    # **实测动因**：人格提示词一直要求「区域三选一必须显式写出」「结构改变就走」
    # 「浮盈后锁住利润」「每次必须做仓位管理结论」，但契约里没有任何字段承载 ——
    # 实测那轮模型在 CoT 里写了「跌破 85560 意味着多头腿结构被破坏 → 离场」，
    # 这句话无处安放、随 CoT 一起消失。而且区域无字段时它只是一个标签：
    # 模型为了能用双止盈把「区间」改判成「趋势」就绕过了「区间禁止 2R」这条规则。
    # 所以 region 不只是记录 —— 它是**代码强制**的校验点。
    "18) [代码强制] **区域与失效锚**："
    "`region` 三选一 trend|range|reversal，**必须与你实际分析一致**；"
    "**region=range 时禁止给 tp2** —— 这不是「拒绝那一档」，而是**整轮计划作废**"
    "（区间只做 scalp，不持有 2R 目标；要给 tp2 就说明这不是区间）。"
    "`invalidation`=前提失效价（触及即视为结构破坏）；"
    "`time_stop_bars`=最大持仓轮数；`give_back_pct`=浮盈回撤阈值(%)；"
    "`risk_pct`=本单实际风险占权益的**百分数**（`0.4` = 0.4%，不是 0.004）。"
    # `rule_ids` 不再要求模型产出：它是**技能体系的产物**（BAN-01 / SB-06 那套编号），
    # 而人格不使用技能时，提示词里没有任何编号体系 —— 无源可引。
    # 实测（2026-10-06，同一份冻结行情各 3 次）：契约一旦要求这个字段，模型就会往里
    # 塞东西。先编 `BEAR-FLAG` / `EMA20-FILTER` 这类假名字；把措辞改成「没编号体系
    # 就留空 `[]`」之后，它又改成塞裸数字 `['3','11','17']` —— **措辞管不住，只能不要求**。
    # 字段本身保留在 Chip 里（可选，默认 `[]`）：将来哪个人格真的列了编号，
    # `loop.py` / `persona/runner.py` 仍会把它写进 journal 与订单上下文。
    "这些字段会写入决策日志与订单上下文 —— **下一轮的你会看到本轮写了什么**，"
    "填了才有跨轮一致性，不填等于每轮从零开始。\n"
    # scenarios 曾列在 Tier-1 里，但**全仓没有任何地方读它**（spec 里写的
    # 「进订单上下文」从未接线）—— 而它是嵌套对象，正是 JSON 解析失败的头号
    # 来源。既然无人消费，就不再要求模型产出（schema 仍接受该字段，不影响旧数据）。
    "**不要写 `scenarios`**：没有消费方，嵌套结构只会增加解析失败风险。"
    "需要写情形应对就写进 `reasoning` 或 `meta`。\n"
    # 实测（2026-10-07）：把品种宇宙换成 SOL 后，账户里遗留的 BTC 挂单被模型判为
    # 「不属于当前宇宙的遗留单」，连续两轮发出 `cancel_all`。账户可能是共享的
    # （多 bot 共账户），宇宙外的单**本来就不是这个 bot 的** —— 撤它们会破坏别人。
    # 这条是 `[AI 判断]`（行为约束），代码层没有对应闸门。
    "19) [AI 判断] **只管理自己的品种**：`【品种宇宙】` 里列出的是你**唯一**该管的 symbol。"
    "账户里其它 symbol 的持仓与挂单**不是你的** —— 它们可能属于同一账户上的另一个 bot 或人工操作。"
    "快照里每行持仓/挂单/保护单都带 `in_universe` 字段：`false` 的那些**不是你的**，只忽略。"
    "**禁止**对宇宙外的 symbol 发 `cancel_*` / `close_*` / `reduce_*` / `modify_tp_sl` / `flatten`，"
    "也**不要**把它们当作「脱离宇宙的遗留单」清理。"
    "看到它们时**只忽略、不写进决策**，更不要因此判断「账户状态异常」。\n"
)

PLAN_SCHEMA_HINT = (
    "allowed_actions="
    + ",".join(sorted(CHIP_ACTIONS))
    + " order_types="
    + ",".join(sorted(CHIP_ORDER_TYPES))
)

SYSTEM_PROMPT = _SYSTEM_HEAD + PLAN_SCHEMA_HINT


def build_system_prompt(strategy_prompt: str, tools_guide: str = "", skill_catalog: str = "") -> str:
    """Fixed contract + optional tool guide (system layer) + skill catalog + strategy persona."""
    head = SYSTEM_PROMPT
    if tools_guide:
        head = head + "\n\n" + tools_guide
    if skill_catalog:
        head = head + "\n\n" + skill_catalog
    return head + "\n\n【策略人格】\n" + strategy_prompt


def load_strategy_prompt(
    path: str | Path | None,
    prompts_root: str | Path | None = None,
    bot_root: str | Path | None = None,
) -> str:
    """Load strategy persona text (role + style). Restricted to prompts/ (no arbitrary FS read).

    Resolution order is cwd-independent when `bot_root` (or `prompts_root`) is given:
    absolute path under prompts/, bot_root/<path>, prompts_root/<path>, prompts_root/<name>.
    """
    if not path:
        return (
            "你是加密货币永续合约策略引擎。"
            "核心原则：保住本金，其次才是收益。"
            "策略：趋势跟随。跟随明显方向，震荡 hold。"
            "有仓时优先管理；新仓必须带 sl。单币单计划。"
        )
    if prompts_root is not None:
        root = Path(prompts_root).expanduser().resolve()
    elif bot_root is not None:
        root = (Path(bot_root).expanduser().resolve() / "prompts")
    else:
        root = (Path.cwd() / "prompts").resolve()
    raw = Path(path)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw.resolve())
    else:
        # Prefer project root over cwd so systemd/cron --root still works
        bases = []
        if bot_root is not None:
            bases.append(Path(bot_root).expanduser().resolve())
        bases.append(Path.cwd())
        for base in bases:
            candidates.append((base / raw).resolve())
        candidates.append((root / raw).resolve())
        candidates.append((root / raw.name).resolve())

    def _under_root(p: Path) -> bool:
        try:
            return p == root or root in p.parents
        except Exception:  # noqa: BLE001
            return False

    for cand in candidates:
        if _under_root(cand) and cand.is_file():
            return cand.read_text(encoding="utf-8")
    raise PermissionError(f"prompt_file must exist under {root}: {path}")


def build_user_prompt(snapshot: dict[str, Any], risk: dict[str, Any], symbols: list[str]) -> str:
    return (
        "【品种宇宙】\n"
        + json.dumps(symbols, ensure_ascii=False)
        + "\n\n【策略风控】\n"
        + json.dumps(risk, ensure_ascii=False)
        + "\n\n【市场与账户快照】\n"
        + json.dumps(snapshot, ensure_ascii=False)
        + "\n\n请输出本轮 Plan JSON（含 memory_refs 引用历史决策）。"
    )


def build_messages(
    strategy_prompt: str,
    snapshot: dict[str, Any],
    risk: dict[str, Any],
    symbols: list[str],
    chart_base64: Optional[str] = None,
    skill_catalog: str = "",
):
    """构建消息列表。chart_base64 非空时附 K 线图（vision 模式）。"""
    system = build_system_prompt(strategy_prompt, skill_catalog=skill_catalog)
    text = build_user_prompt(snapshot, risk, symbols)

    if chart_base64:
        # OpenAI/DeepSeek vision 格式
        user_content: Any = [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {"url": chart_base64},
            },
        ]
    else:
        user_content = text

    return system, user_content
