"""Strategist loop: snapshot → LLM → risk → write inbox.

Triggers (per-bot, configurable):
  - interval_sec 定时（5m/10m/… 任意秒）
  - event_on_kline_close + event_timeframe K 线收盘
  - conditions[]：EMA/ATR/RSI/价格突破等条件事件
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..gate_client import GateClient
from .bridge import chips_to_signal, write_hold_audit, write_signal_file
from .llm_client import LLMClient, LLMConfig, LLMError
from .market import MarketConfig
from .prompt import build_system_prompt, build_user_prompt, load_strategy_prompt
from .risk import RiskConfig, apply_risk
from .schema import PlanError, extract_kline_reads, normalize_chip_type, parse_plan_text
from .snapshot import collect_snapshot
from .symbols import (
    AMBIGUOUS_MULTI,
    AUTO_SINGLE,
    NO_UNIVERSE,
    OUT_OF_UNIVERSE,
    UNIVERSE_EMPTY,
    resolve_symbol_arg,
)
from .tools import NATIVE_TOOLS, TOOL_GUIDE, available_native_tools, extract_tool_calls, filter_tool_schemas, run_tool
from .triggers import check_conditions, parse_conditions
from .trigger_store import AITriggerPolicy, AITriggerStore, TriggerPolicyError, validate_trigger_payload

log = logging.getLogger("omnialpha.strategist")


@dataclass
class StrategistConfig:
    enabled: bool = True
    interval_sec: int = 300
    timeframe: str = "15m"
    event_on_kline_close: bool = True
    # K 收盘 / 条件事件用的周期；默认跟 timeframe
    event_timeframe: Optional[str] = None
    # 条件事件：EMA/ATR/RSI/price_break 等（见 triggers.py）
    conditions: list = field(default_factory=list)
    check_interval_sec: float = 1.0
    ai_triggers: dict = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    prompt_file: str = "prompts/vergex_default.md"
    write_hold: bool = True
    candles: int = 100
    market: MarketConfig = field(default_factory=MarketConfig)
    env: str = "live"
    bot_root: Optional[Path] = None
    risk: RiskConfig = field(default_factory=RiskConfig)
    # 账户级风控（顶层 bot 配置）：执行器用它算权益比例硬顶，策略侧也据此收敛给 AI 的预算
    account_risk: dict = field(default_factory=dict)
    llm: LLMConfig = field(default_factory=LLMConfig)
    # on-demand market tools for the LLM (not a full dump)
    tools: dict = field(default_factory=dict)
    bot_id: str = ""
    # SkillKit：None=默认可见全部；[]=无 skill；[id,...]=白名单
    skills: Optional[list] = None
    # K 线图视觉识别（需模型支持 vision/image input）
    vision: bool = True
    # 发送哪些周期图（多选）；省略 = 只发主周期 timeframe
    # 例: ["5m","15m","1h","4h"]
    vision_timeframes: Optional[list] = None

    def __post_init__(self) -> None:
        if not self.event_timeframe:
            self.event_timeframe = self.timeframe


class PlanRunner:
    def __init__(
        self,
        client: GateClient,
        cfg: StrategistConfig,
        inbox: Path,
        history_dir: Path,
        llm: Optional[LLMClient] = None,
    ):
        self.client = client
        self.cfg = cfg
        self.inbox = inbox
        self.history_dir = history_dir
        self.llm = llm or LLMClient(cfg.llm)
        self.strategy_prompt = load_strategy_prompt(
            cfg.prompt_file or None,
            bot_root=cfg.bot_root,
        )
        self._last_kline_t: Optional[int] = None
        # 逐币基线（多币才用；单币仍走 `_last_kline_t` 以保持历史行为逐字不变）
        self._last_kline_t_by_symbol: dict[str, int] = {}
        # 上一次 _kline_closed() 判定为"收盘"的币（供触发标签取用）
        self._kline_close_symbols: list[str] = []
        self._last_cycle_id: Optional[str] = None
        self._busy = threading.Lock()
        self._conditions = parse_conditions(cfg.conditions, cfg.symbols)
        self._cond_states: dict[str, Any] = {}
        self._last_trigger: str = ""
        self._cycle_file = history_dir / "last_cycle.json"
        self._last_cycle_id = self._load_last_cycle()
        self._ai_policy = AITriggerPolicy(
            enabled=bool((cfg.ai_triggers or {}).get("enabled")),
            allow_types=tuple((cfg.ai_triggers or {}).get("allow_types") or AITriggerPolicy.__dataclass_fields__["allow_types"].default),
            max_active=int((cfg.ai_triggers or {}).get("max_active") or 5),
            default_cooldown_sec=float((cfg.ai_triggers or {}).get("default_cooldown_sec") or 60),
            default_ttl_sec=float((cfg.ai_triggers or {}).get("default_ttl_sec") or 86400),
            allow_modify=bool((cfg.ai_triggers or {}).get("allow_modify", True)),
        )
        self._ai_store = AITriggerStore(self.history_dir / "ai_triggers.json", self._ai_policy)
        # 工具使用统计（每轮累计，随 thinking 落盘）——防 AI 偷懒不查数据
        self.tool_usage: list[dict] = []
        # 本轮发出的 K 线图**元数据**（symbol/周期/成功与否/字节数）——
        # 图片本体（base64）只发给模型，不落 thinking.json。
        self._last_chart_meta: list[dict] = []
        # 本轮快照摘要（设计 S2.4 的 journal `snapshot_digest`）；
        # persona 侧写 journal 时从这里取。
        self.last_snapshot_digest: str = ""

    def _audit_symbol(self, name: str, args: Optional[dict]) -> tuple[str, Optional[str]]:
        """工具调用审计里的 symbol 归因 → `(symbol, note)`；`note=None` = 该工具与币无关。

        **为什么要在审计体里落 symbol**：多币下事后核对「模型是不是拿错币了」，
        只能靠逐条调用记录 —— 工具名 + 参数摘要回答不了「这条 `klines` 查的是哪个币」，
        而 `args` 里的 symbol 在单币宇宙下是**自动补全**的（模型自己没写）。
        所以这里用 `resolve_symbol_arg` 的 note 区分三种情形：显式给了、自动补全、
        越界/漏写。判据与工具层同源（`strategist/symbols.py`）。
        """
        try:
            from .tools import SYMBOL_TOOLS

            if name not in SYMBOL_TOOLS:
                return "", None
            universe = list(getattr(getattr(self, "cfg", None), "symbols", None) or [])
            sym, note = resolve_symbol_arg(args, universe, tool=name)
            return sym, note
        except Exception:  # noqa: BLE001 — 纯观测，异常只意味着「这条没归因」
            return "", None

    def _record_tool_use(self, name: str, args: dict, result: Any,
                         *, raw_args: Optional[dict] = None) -> None:
        """记录一次工具调用：工具名、参数摘要、结果规模，以及完整返回。

        `result_full` 是排查「模型到底拿到了什么」的唯一依据。只存 preview 时，
        核对工具数值（如 SMC 的 structure_scale.atr）只能靠复现，无法判断模型
        是**引用了真实值**还是**自己估算**。

        `raw_args` = **模型原样给出的参数**。`run_tool` 会就地把补全后的 symbol
        写回 `args`（`tools.py:1528`），所以只有原始副本能回答「模型自己写了
        symbol 没有」—— 缺 symbol 率就是靠它算的。
        """
        try:
            text = str(result) if result is not None else ""
            entry = {
                "tool": str(name or ""),
                "args": {k: str(v)[:40] for k, v in (args or {}).items()},
                "result_preview": text[:200],
                "result_full": text[:20000],
                "result_len": len(text),
            }
            sym, note = self._audit_symbol(
                name, raw_args if raw_args is not None else args)
            if note is not None:
                entry["symbol"] = sym
                entry["symbol_note"] = note
            self.tool_usage.append(entry)
        except Exception:  # noqa: BLE001
            pass

    def _load_last_cycle(self) -> Optional[str]:
        try:
            if self._cycle_file.exists():
                data = json.loads(self._cycle_file.read_text(encoding="utf-8"))
                return data.get("cycle_id")
        except Exception:  # noqa: BLE001
            return None
        return None

    def _save_last_cycle(self, cycle_id: str) -> None:
        try:
            self._cycle_file.parent.mkdir(parents=True, exist_ok=True)
            self._cycle_file.write_text(
                json.dumps({"cycle_id": cycle_id, "ts": datetime.now(timezone.utc).isoformat()}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def _prompt_risk(self, snapshot: dict) -> dict:
        """给 AI 的预算取「配置值」与「权益 × max_notional_pct」的较小值。

        执行器的硬闸门是 equity×max_notional_pct（见 executor._check_notional）。
        若只把静态配置值交给 AI，权益一下跌闸门就收紧，AI 仍按旧预算出价 →
        整笔被拒、白烧一轮。这里把闸门算出来一起给，两者就不会再打架。
        """
        cfg_max = self.cfg.risk.max_notional_usd
        raw_pct = (self.cfg.account_risk or {}).get("max_notional_pct")
        try:
            pct = float(raw_pct) if raw_pct is not None else 5.0
        except (TypeError, ValueError):
            pct = 5.0
        equity = 0.0
        try:
            acct = snapshot.get("account") or {}
            equity = float(acct.get("total") or acct.get("balance") or 0)
        except (TypeError, ValueError):
            equity = 0.0
        limit: Optional[float] = cfg_max
        if equity > 0 and pct > 0:
            cap = equity * pct
            limit = cap if limit is None else min(float(limit), cap)
        return {
            "min_confidence": self.cfg.risk.min_confidence,
            "max_notional_usd": round(limit, 2) if limit is not None else None,
            "max_chips": self.cfg.risk.max_chips,
            # 品种宇宙与每币名额也进风控段：名额是按币给的（T8/D8），不告诉模型配额，
            # 它会以为"几条 chip 都能落在同一个币上"，于是把别的币饿死。
            "symbols": list(self.cfg.symbols or []),
            "max_chips_per_symbol": getattr(self.cfg.risk, "max_chips_per_symbol", 1),
            "allow_actions": sorted(self.cfg.risk.allow_actions)
            if self.cfg.risk.allow_actions
            else None,
        }

    def _active_triggers_block(self) -> str:
        """把已生效的自设触发器列给 AI（空则返回 ""）。

        此前生效触发器从未注入 prompt/snapshot，AI 看不见自己的触发状态 →
        既无法避免重复、也无法用 trigger_ops 删旧的 → 5 个槽位填满后每次 add
        都被拒（线上实测 115 次 trigger_rejected，且 5 个触发器全是同质 price_break）。
        """
        try:
            items = self._ai_store.active() if getattr(self, "_ai_store", None) else []
        except Exception:  # noqa: BLE001
            return ""
        if not items:
            return ""
        cap = int(getattr(self._ai_policy, "max_active", 5) or 5)
        lines = [f"- {t.id} {t.type} {t.symbol} {t.params}" for t in items]
        return (
            f"\n【已生效的自设触发器】（{len(items)}/{cap}）\n"
            + "\n".join(lines)
            + "\n换条件请用 trigger_ops remove 旧的再 add；已满就别再 add（会被拒）。"
        )

    def _order_context_for(self, root: Path, bid: str, *,
                           via_group: bool = True) -> Optional[dict]:
        """本 bot 当前持仓的订单上下文（设计 S2.3）。

        订单库是 persona 组共享的（`data/shared/orders/`），匹配规则分两种：

        - `via_group=True`（persona 的 `analyze_once`）：`members` 里的人都算 ——
          组内成员共管同一张单，都要看到它的理由与事件（设计 S2.8）。
        - `via_group=False`（单 bot 的 `run_once`）：**只看 `target_account == bid`** ——
          仓位在那个账户里。按 `members` 匹配会把**别人的仓位**塞进这个 bot 的 prompt
          （实测：`smc-paper` 是 `disc-trio` 的分析成员，独立跑 plan 时被注入了
          `pa-a` 账户上的空单，而它自己账户是平的）。

        有多个 open 单时**按币各取一张**（≤3）：单币正常只有一张；多币下每枚币各有
        自己的理由与前提失效价，只取 `updated_at` 最新那张会让其余币的上下文凭空
        消失（D-19）。单币（或订单记录里没有 symbol）时返回**单张**，形态同改动前。
        """
        try:
            from ..persona.orders import SharedOrderStore
            store = SharedOrderStore(root)

            def _mine(rec: dict) -> bool:
                if rec.get("target_account") == bid:
                    return True
                return via_group and bid in (rec.get("members") or [])

            mine = [r for r in store.list_open() if _mine(r)]
            if not mine:
                return None
            mine.sort(key=lambda r: int(r.get("updated_at") or 0), reverse=True)
            # 按**币**各取一张（≤3）。原先只取 `updated_at` 最新的一张 —— 多币下一轮
            # 可能同时有 2–3 张单，其余币的理由/前提失效价/最近事件在 prompt 里**凭空
            # 消失**（D-19：模型看不到自己上一轮给另一个币定的失效价）。
            # 单币时返回**单张**（形态与改动前逐字一致），多币才包一层容器。
            by_sym: dict[str, dict] = {}
            for r in mine:
                s = str(r.get("symbol") or "").strip()
                if s and s not in by_sym:
                    by_sym[s] = r
                if len(by_sym) >= 3:
                    break
            if len(by_sym) <= 1:
                # 单币，或记录里没有 symbol（老订单库）→ 退回「最新一张」
                pick = next(iter(by_sym.values())) if by_sym else mine[0]
                return store.get_order_context(pick["order_id"])
            many = [store.get_order_context(r["order_id"]) for r in by_sym.values()]
            many = [c for c in many if c]
            return {"orders": many} if many else store.get_order_context(mine[0]["order_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("order context lookup failed: %s", e)
            return None

    def _remember_snapshot_digest(self, snapshot: Any) -> str:
        """记录本轮快照摘要（journal 的 `snapshot_digest` 从这里取，设计 S2.4）。

        **两条路径都要调** —— 只设在 `run_once` 里时，persona 侧写出的 journal
        这一格永远是空的（实测踩到过）。
        """
        try:
            from ..memory import MemoryJournal
            self.last_snapshot_digest = MemoryJournal.snapshot_digest(snapshot)
        except Exception:  # noqa: BLE001
            self.last_snapshot_digest = ""
        return self.last_snapshot_digest

    def _assemble_prompt(self, root: Path, bid: str,
                         system: str, user: str, *,
                         via_group: bool = True) -> tuple[str, str]:
        """按缓存优化顺序组装 system/user（订单上下文 → 近况 → 快照 → 触发器）。

        **单 bot（`run_once`）与多人格（`analyze_once`）共用这一个入口** —— 两条路径
        各自手写拼装时，只接一条就会出现「有记忆的单 bot、没记忆的人格」。

        `via_group` 决定订单上下文的匹配范围（见 `_order_context_for`）：
        单 bot 只看自己账户，人格路径看全组。

        失败不影响本轮：退回未加工的 system/user（少记忆，但不缺席）。
        """
        try:
            from ..memory import build_context
            ctx = build_context(
                root, bid,
                system_prompt=system,
                order_context=self._order_context_for(root, bid, via_group=via_group),
                snapshot_text=user,
                n_recent=3,
                extra_suffix=self._active_triggers_block(),
            )
            return ctx["system"], ctx["user"]
        except Exception as e:  # noqa: BLE001
            log.warning("memory context assembly failed, using plain prompts: %s", e)
            return system, user + self._active_triggers_block()

    def _record_cache_usage(self, root: Path, bid: str, system: str) -> tuple[int, str]:
        """记录本轮 prompt 缓存命中 + 前缀稳定性，返回 (命中token, 模型名)。

        返回值直接喂给 journal（设计 S2.4 的 `prompt_cache_hit_tokens` / `llm_model`），
        这样「缓存命中率」这个成本主杠杆才是可测量的，而不是恒 0。
        """
        hit, model = 0, ""
        try:
            from ..memory import CacheGuard
            if hasattr(self.llm, "cache_hit_tokens"):
                hit = int(self.llm.cache_hit_tokens())
            if hasattr(self.llm, "prompt_tokens"):
                total = int(self.llm.prompt_tokens())
            else:
                total = 0
            model = str(getattr(self.llm, "last_model", "") or "")
            guard = CacheGuard(root, bid)
            guard.record(hit, total, model=model)
            if not guard.check_prefix(system):
                log.warning("memory cache prefix changed this cycle — 缓存命中率会掉")
        except Exception as e:  # noqa: BLE001
            log.warning("cache guard failed: %s", e)
        return hit, model

    def run_once(self, trigger: str = "manual") -> dict[str, Any]:
        # 每轮开始清零。原先只有 `analyze_once` 清 —— plan-loop 走的是 run_once，
        # 于是 `tool_usage` **跨轮累加**，`tool_usage_summary` 的 counts 越跑越大，
        # 「这轮调了几次工具」根本读不出来（D-26）。
        self.tool_usage = []
        # 图元数据同理：vision 关掉时必须清空，否则 thinking.json 会挂着上一轮的图。
        self._last_chart_meta = []
        cycle_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot = collect_snapshot(
            self.client,
            self.cfg.symbols,
            candles=self.cfg.candles,
            interval=self.cfg.timeframe,
            market_cfg=self.cfg.market,
            env=self.cfg.env,
            bot_root=self.cfg.bot_root,
        )
        self._remember_snapshot_digest(snapshot)
        account = snapshot.get("account") or {}
        if account.get("error"):
            # 账户取不到不阻断——行情分析仍可进行（AI 会看到 account 为空）
            log.warning("account unavailable, continue with market-only analysis: %s",
                        account.get("error"))
        system = build_system_prompt(
            self.strategy_prompt,
            tools_guide=TOOL_GUIDE if self.cfg.tools.get("enabled") else "",
            skill_catalog=self._skill_catalog(),
        )
        user = build_user_prompt(
            snapshot,
            self._prompt_risk(snapshot),
            self.cfg.symbols,
        )
        # agent-memory：订单上下文 + 画像 + 近况，按缓存优化顺序拼装（设计 S2.6）
        # 单 bot 路径 → 只看自己账户的仓位（via_group=False）
        root = self.cfg.bot_root or Path.cwd()
        bid = self.cfg.bot_id or self.inbox.name
        system, user = self._assemble_prompt(root, bid, system, user, via_group=False)
        # vision：多周期 K 线图（可选配置）
        charts: list = []
        if self.cfg.vision:
            try:
                charts = self._generate_charts(snapshot)
            except Exception:  # noqa: BLE001
                charts = []
        degraded: Optional[str] = None
        # 缓存护栏：本轮 usage 清零（工具循环会多次调用 LLM，只看单次会漏算）
        try:
            if hasattr(self.llm, "reset_usage"):
                self.llm.reset_usage()
        except Exception:  # noqa: BLE001
            pass
        t_llm = time.time()
        try:
            text = self._chat_with_tools(system, user, chart_base64=charts or None)
        except Exception as e:  # noqa: BLE001 — 网络/模型/工具异常都不该让整轮缺席
            log.exception("plan llm failed: %s", e)
            degraded = f"llm_failed: {e}"
            text = ""
        llm_latency = round(time.time() - t_llm, 1)
        # 缓存护栏：记录 prompt 缓存命中 + 前缀稳定（设计 S2.6 / T7）
        cache_hit, llm_model = self._record_cache_usage(root, bid, system)
        # monitoring: heartbeat（LLM 调用完成；延迟取真实值，别再硬编码 0）
        try:
            from ..monitoring import HealthMonitor
            HealthMonitor(root, bid).heartbeat(
                llm_latency=llm_latency, cycle_id=cycle_id)
        except Exception:  # noqa: BLE001
            pass
        if text:
            try:
                self._save_thinking(
                    cycle_id=cycle_id,
                    trigger=trigger,
                    content=text,
                    reasoning=list(getattr(self.llm, "last_reasoning_chain", []) or []),
                )
            except Exception:  # noqa: BLE001
                pass
        if degraded:
            plan = self._hold_plan(cycle_id, degraded)
        else:
            try:
                plan = parse_plan_text(text, symbols=self.cfg.symbols)
            except PlanError as e:
                log.error("plan parse failed: %s", e)
                degraded = f"parse_failed: {e}"
                plan = self._hold_plan(cycle_id, degraded)
        if degraded:
            self._record_cycle_failure(root, bid, degraded)
        else:
            try:
                from ..monitoring import HealthMonitor
                HealthMonitor(root, bid).record_success()
            except Exception:  # noqa: BLE001
                pass
        tail_extra = {"degraded": degraded} if degraded else {}
        if not plan.cycle_id:
            plan.cycle_id = cycle_id
        if plan.cycle_id == self._last_cycle_id:
            return {
                "ok": True,
                "cycle_id": plan.cycle_id,
                "skipped": "duplicate_cycle",
                "orders": 0,
                "trigger": trigger,
                **tail_extra,
            }
        self._last_cycle_id = plan.cycle_id
        self._save_last_cycle(plan.cycle_id)
        rejected_triggers = self._apply_ai_triggers(plan)
        risk_result = apply_risk(plan, self.cfg.risk)
        payload = chips_to_signal(plan, risk_result, bot_id=self.cfg.bot_id or self.inbox.name)
        orders = payload.get("orders") or []
        # agent-memory：每轮决策入 journal（含快照摘要/模型/缓存命中，设计 S2.4）
        try:
            from ..memory import MemoryJournal
            acts = [c.action for c in plan.chips]
            MemoryJournal(root, bid).append(
                cycle_id=plan.cycle_id,
                decision=",".join(acts) or "hold",
                reasoning=plan.reasoning[:500],
                # 模型自报的本轮引用（prompt 一直在要求它输出）。原先没传这个参数，
                # 于是字段在 journal 里恒为空 —— 实测 896 条记录非空 0 条。
                memory_refs=list(getattr(plan, "memory_refs", None) or []),
                snapshot_digest=self.last_snapshot_digest,
                llm_model=llm_model,
                prompt_cache_hit_tokens=cache_hit,
                executed=bool(orders),
                exec_result={"orders": len(orders), "trigger": trigger},
                **self._tier1_journal_fields(plan),
            )
        except Exception:  # noqa: BLE001
            pass
        # agent-memory：遗忘机制（TTL 归档 + 超龄已平仓清理，设计 S2.7）
        # 内部按间隔（默认 24h）判断，未到点直接返回 —— 每轮调是安全的。
        try:
            from ..memory import run_gc
            gc = run_gc(root, bot_id=bid)
            if gc.get("ran") and (gc.get("archived") or gc.get("removed")):
                log.info("memory gc: archived=%s removed=%s",
                         gc.get("archived"), gc.get("removed"))
        except Exception as e:  # noqa: BLE001
            log.warning("memory gc failed: %s", e)
        if not orders:
            if self.cfg.write_hold:
                write_hold_audit(self.history_dir, plan, cycle_id=plan.cycle_id)
            self._record_plan(cycle_id=plan.cycle_id, trigger=trigger, orders=0,
                              notes=risk_result.notes + rejected_triggers,
                              reasoning=plan.reasoning)
            # monitoring: decay（hold 不计盈亏）
            self._record_decay(plan, executed=False, equity=self._decay_equity(snapshot))
            return {
                "ok": True,
                "cycle_id": plan.cycle_id,
                "orders": 0,
                "notes": risk_result.notes + rejected_triggers,
                "rejected": len(risk_result.rejected),
                "trigger": trigger,
                **tail_extra,
            }
        path = write_signal_file(self.inbox, payload, cycle_id=plan.cycle_id)
        self._record_plan(cycle_id=plan.cycle_id, trigger=trigger, orders=len(orders),
                          notes=risk_result.notes + rejected_triggers, reasoning=plan.reasoning)
        # monitoring: decay（执行后按方向记 PnL）
        self._record_decay(plan, executed=True, equity=self._decay_equity(snapshot))
        return {
            "ok": True,
            "cycle_id": plan.cycle_id,
            "orders": len(orders),
            "file": str(path),
            "notes": risk_result.notes + rejected_triggers,
            "rejected": len(risk_result.rejected),
            "trigger": trigger,
        }


    @staticmethod
    def _tier1_journal_fields(plan: Any) -> dict:
        """契约 Tier 1 字段进 journal —— **按币**形态（见 `memory/journal.py`）。

        多币下每枚币各有自己的 region/invalidation/risk_pct，只取 `chips[0]` 会让
        其余币的判定凭空消失（审计 D-21）。实现只有一处（`tier1_journal_fields`），
        与 persona 路径共用 —— 此前两边各写一份、已经漂移过一次。
        """
        from ..memory.journal import tier1_journal_fields

        return tier1_journal_fields(getattr(plan, "chips", None) or [])
    def _skill_catalog(self) -> str:
        """SkillKit L1 catalog：仅 name+description，空则不渲染。"""
        try:
            from ..skillkit import SkillRegistry, render_catalog

            root = Path(self.cfg.bot_root) if self.cfg.bot_root else Path.cwd()
            reg = SkillRegistry()
            reg.scan([root / ".mimocode" / "skills", root / "skills"])
            enabled = getattr(self.cfg, "skills", None)
            metas = reg.visible_for(self.cfg.bot_id or "", enabled)
            return render_catalog(metas)
        except Exception:  # noqa: BLE001
            return ""

    def _hold_plan(self, cycle_id: str, reason: str) -> Plan:
        """降级用的 hold Plan：让本轮照常走完风控/记录链路，不留下空周期。

        **逐币**而不是只给首币：多币宇宙里"只给首币一条 hold"会让其余币在
        journal/近况里**完全缺席**，模型下一轮看不到它们（等于那几枚币被静默丢出决策）。
        宇宙为空时不再兜底 `BTC_USDT` —— 那是在猜一个本 bot 根本没配的标的。
        """
        from .schema import Chip, Plan

        syms = list(self.cfg.symbols or [])
        chips = [Chip(symbol=s, action="hold", confidence=0.0,
                      reasoning="数据/模型异常，降级观望") for s in syms]
        # 宇宙为空（本 bot 未配置 symbols）时**不产出 chip**：一条 `symbol=""` 的 hold
        # 会让下游以为「本轮对某个币表了态」，而那个币根本不存在。留痕走 `raw.degraded`。
        return Plan(
            cycle_id=cycle_id,
            reasoning=f"[降级] {reason[:60]}",
            chips=chips,
            raw={"degraded": reason},
        )

    def _record_cycle_failure(self, root: Path, bid: str, detail: str) -> None:
        """记一次周期失败（落盘 + 连续失败告警）；实盘额外推飞书。"""
        try:
            from ..monitoring import HealthMonitor

            hm = HealthMonitor(root, bid)
            streak = hm.record_error(detail=detail)
        except Exception:  # noqa: BLE001
            return
        if not streak or streak % hm.error_warn:
            return
        try:
            from ..monitoring import notify_process_event, should_notify

            if not should_notify(root, self.cfg.env):
                return
            notify_process_event(
                root=root, kind="plan_fail",
                title=f"plan 连续失败 {streak} 次",
                fields=[("Bot", bid), ("连续失败", str(streak)),
                        ("最近原因", detail[:80])],
                color="red",
            )
        except Exception:  # noqa: BLE001
            pass

    def _hold_fallback(self, cycle_id: str, trigger: str, reason: str) -> dict:
        """降级：LLM/解析失败时不丢票——返回 hold 计划，标记 degraded。

        稳定优先：一个成员失败不应让它整轮缺席（否则融合只剩少数人）。
        """
        log.warning("degrade to hold: %s", reason)
        # 与 `_hold_plan` 同源：多币逐币一条 hold；宇宙为空时不产出 chip（留痕走 meta）
        syms = list(self.cfg.symbols or [])
        chips = [{"symbol": s, "action": "hold", "confidence": 0.0,
                  "reasoning": "数据/模型异常，降级观望"} for s in syms]
        return {
            "ok": True,
            "degraded": reason,
            "cycle_id": cycle_id,
            "trigger": trigger,
            "plan": {
                "cycle_id": cycle_id,
                "reasoning": f"[降级] {reason[:40]}",
                "chips": chips,
                "meta": {"degraded": reason},
            },
        }

    def analyze_once(self, trigger: str = "manual") -> dict[str, Any]:
        """各人格独立分析：LLM → Plan，不写 inbox、不执行（供多人格融合）。"""
        self.tool_usage = []  # 每轮开始时清零
        self._last_chart_meta = []  # 图元数据同理（否则挂上一轮的图）
        cycle_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot = collect_snapshot(
            self.client,
            self.cfg.symbols,
            candles=self.cfg.candles,
            interval=self.cfg.timeframe,
            market_cfg=self.cfg.market,
            env=self.cfg.env,
            bot_root=self.cfg.bot_root,
        )
        self._remember_snapshot_digest(snapshot)
        account = snapshot.get("account") or {}
        if account.get("error"):
            # 账户取不到不阻断：多人格里仍给出行情观点，不掉票
            log.warning("account unavailable (persona), market-only: %s", account.get("error"))
        system = build_system_prompt(
            self.strategy_prompt,
            tools_guide=TOOL_GUIDE if self.cfg.tools.get("enabled") else "",
            skill_catalog=self._skill_catalog(),
        )
        user = build_user_prompt(
            snapshot,
            self._prompt_risk(snapshot),
            self.cfg.symbols,
        )
        # agent-memory：与 run_once 共用同一套组装（订单上下文 + 画像 + 近况 + 触发器）。
        # 人格组正是**持有订单记录**的那条路径 —— 这里漏掉就等于「有记忆的单 bot、
        # 没记忆的人格」，而画像/近况本来就为持仓管理而设计。
        root = self.cfg.bot_root or Path.cwd()
        bid = self.cfg.bot_id or self.inbox.name
        system, user = self._assemble_prompt(root, bid, system, user)
        # vision：从 snapshot 生成 K 线图（可配置多周期）
        charts: list = []
        if self.cfg.vision:
            try:
                charts = self._generate_charts(snapshot)
            except Exception:  # noqa: BLE001
                charts = []
        # 缓存护栏：本轮 usage 清零（工具循环会多次调用 LLM，只看单次会漏算）
        try:
            if hasattr(self.llm, "reset_usage"):
                self.llm.reset_usage()
        except Exception:  # noqa: BLE001
            pass
        try:
            text = self._chat_with_tools(system, user, chart_base64=charts or None)
            # 缓存护栏：记录本轮命中 + 前缀稳定（设计 S2.6 / T7）
            self._record_cache_usage(root, bid, system)
            # 与 run_once 对齐：必须把 CoT 与正文一起落盘。
            # 原先这里只传 system/user/text，而 _save_thinking 读的是 `content` / `reasoning`，
            # 于是**多人格模式（persona-run 走 analyze_once）的 thinking.json 里
            # reasoning_chain 与 content_head 双双为空** —— 2KB vs 单 bot 的 27KB。
            # 后果：多人格讨论的「分析过程」不可审计，只有 plan.reasoning 那一句话。
            self._save_thinking(
                cycle_id=cycle_id,
                trigger=trigger,
                content=text,
                reasoning=list(getattr(self.llm, "last_reasoning_chain", []) or []),
            )
        except Exception as e:  # noqa: BLE001
            log.error("analyze llm failed: %s", e)
            return self._hold_fallback(cycle_id, trigger, f"llm_failed: {e}")
        try:
            plan = parse_plan_text(text, symbols=self.cfg.symbols)
        except PlanError as e:
            log.error("analyze plan parse failed: %s", e)
            return self._hold_fallback(cycle_id, trigger, f"parse_failed: {e}")
        if not plan.cycle_id:
            plan.cycle_id = cycle_id
        # 给融合层的紧凑 Plan（不写 inbox）
        chips_out = []
        for c in getattr(plan, "chips", []) or []:
            # 用 Chip 自己的序列化 —— 它含执行必需字段（`trigger_price` / `price` /
            # `side` / `leverage` …）。原先手抄 7 个字段，把 stop_entry_* 的
            # `trigger_price` 丢在**源头**，于是人格永远挂不出突破单：executor 一律以
            # "stop_entry_short requires trigger_price (breakout level)" 拒掉。
            try:
                d = c.to_signal_dict()
            except Exception:  # noqa: BLE001
                d = {"symbol": getattr(c, "symbol", ""), "action": getattr(c, "action", "")}
            d["confidence"] = getattr(c, "confidence", 0.0)
            d["reasoning"] = getattr(c, "reasoning", "")
            chips_out.append(d)
        return {
            "ok": True,
            "cycle_id": plan.cycle_id,
            "trigger": trigger,
            "plan": {
                "cycle_id": plan.cycle_id,
                "reasoning": plan.reasoning,
                "chips": chips_out,
                "decision": chips_out[0]["action"] if chips_out else "hold",
                "confidence": chips_out[0]["confidence"] if chips_out else 0.0,
                # 模型自报的本轮引用 —— persona 路径的 journal 写入从这里取
                # （`persona/runner.py::_post_exec_hooks`），否则那条路径拿不到它。
                "memory_refs": list(getattr(plan, "memory_refs", None) or []),
            },
        }

    @staticmethod
    def _decay_equity(snapshot: Any) -> Optional[float]:
        """取给 decay 用的权益 —— **只在空仓时给**。

        权益差是 plan-loop 手边唯一可得的盈亏代理（它不执行订单、拿不到已实现盈亏，
        journal 的 `exec_result` 里也没有 pnl）。但权益差含**未实现盈亏**：持仓浮亏 5U
        而没平仓时权益差就是 −5，会让「衰减」比真实情况更早触发。

        所以只在**空仓**时给出权益：每笔记录的差值 = 「自上次空仓以来的权益变化」
        ≈ 该笔持仓平掉后的已实现盈亏（含手续费/资金费），持仓期间记 0。
        等价于「按笔记已实现盈亏」，而不是「按周期记浮动的权益」。
        """
        try:
            acct = (snapshot or {}).get("account") or {}
            if acct.get("error"):
                return None
            if acct.get("positions"):
                return None          # 持仓中 → 不记，避免未实现盈亏污染
            v = acct.get("total") or acct.get("balance")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _record_decay(self, plan: Any, executed: bool, equity: Optional[float] = None) -> None:
        """monitoring: 每轮记录到 decay detector。

        盈亏不在这里硬编码 —— 由 detector 用**权益差**推算（见 DecayDetector.record_cycle）。
        此前两个调用点都传 `pnl_usd=0.0`，导致滚动盈亏/胜率/Sharpe 恒为 0、衰减告警永不触发。
        """
        try:
            from ..monitoring import DecayDetector
            root = self.cfg.bot_root or Path.cwd()
            bid = self.cfg.bot_id or self.inbox.name
            det = DecayDetector(root, bid)
            acts = [c.action for c in (plan.chips or [])]
            # 本轮只涉及**一个**币时记下它（多币时留空）：权益差是账户级口径，
            # 归不到某个币头上 —— 硬塞一个币名会让衰减告警指错人。
            # 单币时带上它，多币场景下的按币衰减才有数据可用（T13）。
            _syms = {str(getattr(c, "symbol", "") or "") for c in (plan.chips or [])}
            _syms.discard("")
            det.record_cycle(
                cycle_id=plan.cycle_id or '',
                decision=','.join(acts) or 'hold',
                executed=executed, equity=equity,
                symbol=next(iter(_syms)) if len(_syms) == 1 else "",
            )
            alert = det.check()
            if alert:
                log.warning('decay alert: %s', alert)
                # 发到飞书/Telegram（可选）
                try:
                    from ..monitoring import AlertNotifier, FeishuChannel
                    import os
                    feishu_webhook = os.environ.get('FEISHU_WEBHOOK')
                    feishu_app_id = os.environ.get('FEISHU_APP_ID')
                    feishu_app_secret = os.environ.get('FEISHU_APP_SECRET')
                    feishu_user = os.environ.get('FEISHU_USER_OPEN_ID')
                    n = AlertNotifier()
                    if feishu_webhook:
                        n.register(FeishuChannel(webhook=feishu_webhook))
                    elif feishu_app_id and feishu_app_secret and feishu_user:
                        n.register(FeishuChannel(app_id=feishu_app_id,
                                                  app_secret=feishu_app_secret,
                                                  user_open_id=feishu_user))
                    if n.has_channel:
                        n.send('decay: ' + '; '.join(alert.get('alerts', [])))
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def discuss(self, plan: dict, peers: list, round_num: int,
                discussion_text: str, max_rounds: int = 3) -> Optional[dict]:
        """多人格讨论：基于他人观点修订决策（三阶段递进）。

        阶段语义（按**实际轮数**划分，不是固定轮号；`stage = min(round_num, max_rounds)`）：
          首轮 = 相互讨论与反驳：看对方理由，可反驳、可被说服
          中间轮 = 深化讨论：聚焦分歧点，尝试达成一致
          末轮 = 最终决策：定稿，给出最终 decision + 置信度

        返回 {decision, confidence, reasoning} 或 None（无法修订）。
        """
        if self.llm is None:
            return None
        stage = min(round_num, max_rounds)
        if stage == 1:
            stage_hint = (
                "这是【相互讨论与反驳】阶段。请看同伴观点，若有分歧请明确反驳理由；"
                "若对方有理请修改自己的决策。保持独立判断，不盲从多数。"
            )
        elif stage >= max_rounds:
            stage_hint = (
                "这是【最终决策】阶段。综合全部讨论后给出你的最终决定。"
                "必须定稿一个决策，不要再摇摆。"
            )
        else:
            stage_hint = (
                "这是【深化讨论】阶段。聚焦未解决的分歧，尝试找到更高置信度的共识。"
            )

        my_decision = plan.get("decision") or "hold"
        my_reasoning = str(plan.get("reasoning") or "")[:200]
        # 单币宇宙：契约**逐字不变**（I11）—— 标的唯一，凭空加个 `symbol` 字段只会
        # 让缓存前缀无谓变化。多币宇宙：必须写 `symbol` 并把可选值列出来，否则讨论
        # 结论归不到任何一个币上（补错标的比少一条结论更危险）。
        universe = [str(x) for x in (self.cfg.symbols or [])]
        multi = len(universe) > 1
        sym_field = ('"symbol":"%s",' % "|".join(universe)) if multi else ""
        sym_rule = (
            "；6) **symbol 必须写、且必须是【品种宇宙】里的那个**（%s）—— "
            "讨论结论是针对哪个币的，缺了或写错整条就作废"
            "（系统不会替你补，补错标的比少一条更危险）" % "、".join(universe)
            if multi else ""
        )
        sys_prompt = (
            "你是交易策略人格，参与多空讨论。只输出一个 JSON 对象，不要 Markdown 前后缀。\n"
            f'{{{sym_field}"decision":"open_long|open_short|close|reduce_long|reduce_short|hold|'
            'stop_entry_long|stop_entry_short",'
            '"confidence":0.0,"reasoning":"≤30字",'
            '"type":"limit|market|post_only|ioc|fok","price":0.0,"trigger_price":0.0,'
            '"sl":0.0,"tp":0.0,"size_usd":0.0}\n'
            f"{stage_hint}\n"
            "规则：1) decision 用英文枚举；2) confidence 0~1；3) reasoning ≤30 字；"
            "4) **入场类（open_*/stop_entry_*）必须给出 sl 与可执行价位**"
            "（限价给 price，突破进场给 trigger_price）；"
            "5) 给不出可执行价位就选 hold —— 讨论的结论要能直接执行，不是只表个态"
            f"{sym_rule}。"
        )
        user_prompt = (
            f"我的当前决策：{my_decision}\n我的理由：{my_reasoning}\n\n"
            f"同伴观点：\n{discussion_text}\n\n"
            f"讨论轮次：第 {stage}/{max_rounds} 轮。请输出修订后的决策 JSON。"
        )
        try:
            raw = self.llm.chat(sys_prompt, user_prompt)
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        # 讨论轮的 LLM 调用**落盘**。原先完全不落 —— 只有截断到 30 字的
        # reasoning 进 `discussion_log`，事后无法核对「模型当时看到什么同伴
        # 观点、回了什么」。这与「LLM 理由文本不是审计证据」直接冲突。
        self._log_discussion_call(round_num, sys_prompt, user_prompt, raw)
        try:
            import json as _json
            import re as _re
            text = str(raw).strip()
            m = _re.search(r"\{.*\}", text, _re.DOTALL)
            if not m:
                return None
            data = _json.loads(m.group(0))
            decision = str(data.get("decision") or my_decision).lower()
            allowed = {"open_long", "open_short", "close", "reduce_long", "reduce_short",
                       "hold", "stop_entry_long", "stop_entry_short", "close_all", "flatten"}
            if decision not in allowed:
                decision = my_decision
            conf = data.get("confidence")
            try:
                conf = max(0.0, min(1.0, float(conf))) if conf is not None else plan.get("confidence")
            except (TypeError, ValueError):
                conf = plan.get("confidence")
            reasoning = str(data.get("reasoning") or my_reasoning)[:30]
            # 讨论必须产出**可执行**的结论。只改 decision 而不改 chips 的话，
            # fuse_plans._norm_dir 与 _execute 都优先读 chips[0].action ——
            # 讨论结果根本进不了融合（线上实测：三人讨论后都改成 stop_entry_long，
            # 融合票却仍是讨论前的 hold/long/hold，讨论成了纯日志表演）。
            # 标的先独立判（不能只看 `_discussion_chip` 的返回值：**越界**与"缺 sl"
            # 都表现为 None，而留痕需要区分原因）。取值链与 `_discussion_chip` 一致：
            # 原 chip → 模型这次写的 → 无。缺或越界都退回 hold，不替它挑。
            base_chips = plan.get("chips") or []
            base_sym = (str((base_chips[0] or {}).get("symbol") or "")
                        if base_chips and isinstance(base_chips[0], dict) else "")
            given = str(data.get("symbol") or base_sym or "").strip().upper()
            meta: Optional[dict] = None
            if not given:
                meta = {"reason": "symbol_missing"}
            elif universe and given not in universe:
                meta = {"reason": "symbol_not_in_universe", "corrected_from": given}
            chip = None if meta else self._discussion_chip(
                data, decision, plan, default_symbol=self._default_symbol(),
                allowed=self.cfg.symbols)
            if meta is not None:
                decision = "hold"
            elif decision in ("open_long", "open_short", "stop_entry_long", "stop_entry_short") \
                    and chip is None:
                decision = "hold"   # 入场却给不出可执行价位 → 退回 hold，不假装能执行
            out = {"decision": decision, "confidence": conf,
                   "reasoning": reasoning, "chip": chip}
            if meta:
                out["meta"] = meta
            return out
        except Exception:  # noqa: BLE001
            return None

    def _default_symbol(self) -> str:
        """本 bot 的第一个标的 —— 讨论产出的 chip 缺 symbol 时的兜底。

        原先这条兜底是 `_discussion_chip` 里的硬编码 `"BTC_USDT"`，对只做 ETH 的组
        会把标的写错（实测 eth-disc 三人格只做 ETH，产出的信号却带 `BTC_USDT`，
        而价位是 ETH 的 2694/2706）。

        **多币宇宙返回空串**：那时"首币"不是唯一解，补上去就是张冠李戴
        （结论可能针对别的币），让调用方拒绝该 chip 比猜一个更安全。
        """
        syms = getattr(self.cfg, "symbols", None) or []
        return str(syms[0]) if len(syms) == 1 else ""

    def _log_discussion_call(self, round_num: int, sys_prompt: str,
                             user_prompt: str, raw: str) -> None:
        """讨论轮的 LLM 调用留痕（append-only jsonl）。

        与 `_save_thinking` 的区别：那个存的是**独立分析**轮的 CoT；
        讨论轮走的是另一条调用路径（纯文本、无工具），原先两条都不落。
        """
        try:
            out = self.history_dir
            out.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": time.time(),
                "round": int(round_num),
                "system": sys_prompt,
                "user": user_prompt,
                "raw": str(raw),
            }
            with (out / "discussion_calls.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _discussion_chip(data: dict, decision: str, plan: dict,
                         default_symbol: str = "",
                         allowed: Optional[list] = None) -> Optional[dict]:
        """把讨论结论拼成可执行 chip；入场类缺 sl / 价位则返回 None（调用方退回 hold）。

        `default_symbol` 由调用方传 bot 自己的 `symbols[0]`（见 `_default_symbol`）；
        `allowed` = 品种宇宙，**越界一律拒绝**该 chip。

        为什么越界要拒而不是改成首币：讨论结论可能是针对**另一个币**的，补成首币
        等于把结论张冠李戴地执行（原实现就是 `or "BTC_USDT"`，实测 eth-disc 只做 ETH
        却产出带 `BTC_USDT` 的信号）。宁可少一条 chip，也不要下错标的的单。
        """
        chips = plan.get("chips") or []
        base = dict(chips[0]) if chips and isinstance(chips[0], dict) else {}
        if decision == "hold":
            return None

        def num(key):
            try:
                v = data.get(key)
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        chip = dict(base)
        chip["action"] = decision
        # 标的取值链：**原 chip**（已有持仓/计划的标的）→ 模型这次写的 `data.symbol`
        # → 调用方给的 default_symbol。漏掉中间这一环，模型在讨论里明确写的币会被
        # 静默丢掉，再被 default_symbol 覆盖 —— 那正是"改口成别的币却没生效"。
        chip["symbol"] = str(
            chip.get("symbol") or data.get("symbol") or default_symbol or ""
        ).strip().upper()
        universe = [str(x) for x in (allowed or [])]
        if universe and chip["symbol"] and chip["symbol"] not in universe:
            return None          # 越界 → 拒绝，不做纠正（结论可能本来就是别的币的）
        # 缺 symbol **不在这里拒**：本函数看不到调用方上下文，留空交调用方判定
        # （`discuss` 会记 `symbol_missing` 并退回 hold）—— 补一个首币等于张冠李戴。
        if decision not in ("open_long", "open_short", "stop_entry_long", "stop_entry_short"):
            return chip          # close/reduce/modify 沿用原 chip 的执行字段
        sl = num("sl")
        if sl is None or sl <= 0:
            return None          # 入场没止损 → 不可执行
        chip["sl"] = sl
        for key in ("price", "trigger_price", "tp", "size_usd"):
            v = num(key)
            if v is not None and v > 0:
                chip[key] = v
        # 讨论环节的 type 必须走同一套归一 —— 它绕过了 `parse_plan`，
        # 模型在这里写 `stop_market` 会一路进到信号里，被 executor 以
        # `unsupported type: 'stop_market'` 整笔拒掉（线上实测踩到）。
        chip["type"] = normalize_chip_type(data.get("type") or chip.get("type")) or "market"
        return chip

    def _tool_usage_summary(self) -> dict:
        """工具使用摘要：按工具名 + **按币**（设计 S2.4⑨ / D-25）。

        旧字段（`counts`/`total_calls`/`data_checked`）语义与取值逐字不变；
        新增三个只在多币下才有意义的观测：

        - `by_symbol`        按**解析后**的 symbol 归因：每个币调了多少次、用了哪些工具
                             （含单币自动补全 —— 它描述的是「数据取自哪个币」）
        - `missing_symbol`   模型**没显式写 symbol** 的次数（`auto_single` 也算：
                             单币下无歧义，但币一多这些调用就会变成拒绝 —— 最早的预警）
        - `out_of_universe`  写了 symbol 但**不在宇宙内**（错币/幻觉币）的次数

        没有这三个数，多币下「模型拿错币」只能靠翻 `result_full` 逐条看。
        """
        counts: dict[str, int] = {}
        by_symbol: dict[str, dict] = {}
        missing = 0
        out_of_universe = 0
        for u in self.tool_usage:
            t = u.get("tool") or "?"
            counts[t] = counts.get(t, 0) + 1
            note = u.get("symbol_note")
            if note is None:  # 与币无关的工具（skill / journal_lookup / …）
                continue
            sym = str(u.get("symbol") or "")
            if note == OUT_OF_UNIVERSE:
                out_of_universe += 1
            elif note in (AUTO_SINGLE, AMBIGUOUS_MULTI, UNIVERSE_EMPTY, NO_UNIVERSE):
                missing += 1
            if not sym:
                continue
            rec = by_symbol.setdefault(sym, {"calls": 0, "tools": []})
            rec["calls"] += 1
            if t not in rec["tools"]:
                rec["tools"].append(t)
        data_tools = {"klines", "indicators", "ticker", "orderbook", "contract",
                      "stats", "account", "smc_map", "smc_events", "sqzmom"}
        return {
            "counts": counts,
            "total_calls": len(self.tool_usage),
            "data_checked": bool(data_tools.intersection(counts.keys())),
            "by_symbol": by_symbol,
            "missing_symbol": missing,
            "out_of_universe": out_of_universe,
        }

    def _save_thinking(self, **kw: Any) -> None:
        """Persist LLM reasoning_content (CoT) for audit/replay."""
        from datetime import datetime

        out = self.history_dir
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        cid = str(kw.get("cycle_id") or "cycle").replace(":", "").replace("/", "-")[:40]
        path = out / f"{ts}-{cid}.thinking.json"
        payload = {
            "ts": time.time(),
            "cycle_id": kw.get("cycle_id"),
            "trigger": kw.get("trigger"),
            "reasoning_chain": kw.get("reasoning") or [],
            # 正文（Plan JSON）**不截断**：原先截 500 字符，长 Plan 会被腰斩，
            # 事后核对「模型到底输出了什么」时看不到尾部（实测踩到 —— 只有
            # 500 字符的 Plan 无法判断字段是否完整）。留 64k 只是防极端膨胀。
            "content_head": (kw.get("content") or "")[:65536],
            "tool_usage": list(self.tool_usage),  # 本轮工具使用明细（防偷懒）
            "tool_usage_summary": self._tool_usage_summary(),
            # 本轮发给模型的 K 线图**元数据**（symbol/周期/是否生成成功/字节数）。
            # 只写元数据：图片本体是 base64（每张几十到几百 KB），写进来会把
            # thinking.json 撑爆。此前 payload 里完全没有 charts 字段 →
            # 「这轮到底给模型发了哪几个币哪些周期的图」事后无从核对（D-24）。
            "charts": [dict(c) for c in (getattr(self, "_last_chart_meta", None) or [])],
        }
        # 逐K形态读（人格可选产出，见 prompts/brooks_btc_pa.md）：**非空才落**。
        # 没被要求这个字段的 bot 不会填，于是它们的 thinking.json 与改动前逐字节一致
        # —— 这是「只影响 brooks-btc」的落盘侧保证。
        kline = extract_kline_reads(kw.get("content") or "")
        if kline:
            payload["kline"] = kline
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # also into sqlite ledger
        try:
            from ..ledger import Ledger, default_ledger_path

            root = self.cfg.bot_root or Path.cwd()
            led = Ledger(default_ledger_path(root))
            led.insert_plan(
                self.inbox.name,
                cycle_id=kw.get("cycle_id"),
                trigger=kw.get("trigger"),
                orders=None,
                notes=["thinking_saved:" + path.name],
                reasoning=" | ".join((kw.get("reasoning") or [""])[:1])[:200],
                raw={"thinking_file": path.name,
                     "reasoning_chain": kw.get("reasoning") or [],
                     "content_head": (kw.get("content") or "")[:200]},
                # 按币维度必须真的写进去（否则这一列恒 NULL，按币查询形同虚设）
                symbols=self._plan_symbols(),
            )
            led.close()
        except Exception:  # noqa: BLE001
            pass

    def _record_plan(self, **kw: Any) -> None:
        try:
            from ..ledger import Ledger, default_ledger_path

            root = self.cfg.bot_root or Path.cwd()
            led = Ledger(default_ledger_path(root))
            kw.setdefault("symbols", self._plan_symbols())
            led.insert_plan(self.inbox.name, **kw)
            led.close()
        except Exception:  # noqa: BLE001
            pass

    def _plan_symbols(self) -> list:
        """本 bot 的币集 —— 写进 ledger 的 `plans.symbols_json`。

        用**宇宙**而不是「本轮 chips 的币」：`_record_plan` 拿不到 plan 对象，
        而 `plans` 表的用途是「按币查这个 bot 的计划记录」—— 宇宙足够且稳定。
        不接线的话这一列在生产里**恒为 NULL**，`recent_plans(symbol=...)` 永远筛不到
        （T15 就在修这个形态，别把它复刻一遍）。
        """
        return [str(s) for s in (getattr(self.cfg, "symbols", None) or []) if s]

    def _llm_send(self, messages: list) -> str:
        if hasattr(self.llm, "chat_messages"):
            return self.llm.chat_messages(messages)
        # test doubles / older clients: only chat(system, user)
        system = messages[0]["content"] if messages else ""
        user = "\n\n".join(
            m.get("content") or "" for m in messages[1:] if m.get("role") == "user"
        )
        return self.llm.chat(system, user)

    def _generate_charts(self, snapshot: dict) -> list:
        """按 vision_timeframes 生成多周期 K 线图 base64 列表（**逐 symbol**）。

        省略 vision_timeframes → 只发主周期。

        **必须逐 symbol**：原先只给 `symbols[0]` 出图，多币种 bot 的第 2..N 个币
        一张图都没有 —— 模型只能靠数值快照判断它们，等于「多币配置、单币视野」。
        图内自带 symbol 与周期标注（`vision.generate_and_encode(symbol=…, timeframe=…)`），
        所以顺序只决定发送次序、不决定归属。

        每张图（含**没生成成功**的）都会记一条元数据到 `self._last_chart_meta`，
        由 `_save_thinking` 落进 thinking.json —— 只写元数据，不写 base64 本体。
        """
        try:
            from .vision import generate_and_encode
        except Exception as e:  # noqa: BLE001
            log.warning("vision 模块不可用，本轮不发 K 线图: %s", e)
            return []
        symbols = self.cfg.symbols or []
        if not symbols:
            return []
        market = snapshot.get("market") or {}
        tfs = list(self.cfg.vision_timeframes or []) or [self.cfg.timeframe]
        # 去重保序
        seen = set()
        tfs = [t for t in tfs if t and not (t in seen or seen.add(t))]
        out: list = []
        meta: list[dict] = []
        for sym in symbols:
            m = market.get(sym) or {}
            if not isinstance(m, dict):
                m = {}
            made = 0
            for tf in tfs:
                try:
                    if tf == self.cfg.timeframe:
                        klines = m.get("candles") or m.get("klines") or []
                        ind = m.get("indicators") or {}
                    else:
                        block = (m.get("tf") or {}).get(tf) or {}
                        klines = block.get("candles") or block.get("klines") or []
                        ind = block.get("indicators") or {}
                    if isinstance(klines, dict):
                        klines = klines.get("candles") or klines.get("klines") or []
                    if not klines or len(klines) < 10:
                        meta.append({"symbol": sym, "timeframe": tf, "ok": False,
                                     "reason": "no_candles"})
                        continue
                    merged = []
                    for i, k in enumerate(klines):
                        row = dict(k) if isinstance(k, dict) else {"c": k}
                        for name, series in (ind or {}).items():
                            if isinstance(series, (list, tuple)) and i < len(series):
                                row.setdefault(name, series[i])
                        merged.append(row)
                    b64 = generate_and_encode(merged, symbol=sym, timeframe=tf)
                    if b64:
                        out.append(b64)
                        made += 1
                        # 只记长度，不记 base64 本体
                        meta.append({"symbol": sym, "timeframe": tf, "ok": True,
                                     "bytes": len(str(b64))})
                    else:
                        meta.append({"symbol": sym, "timeframe": tf, "ok": False,
                                     "reason": "empty"})
                except Exception as e:  # noqa: BLE001
                    meta.append({"symbol": sym, "timeframe": tf, "ok": False,
                                 "reason": str(e)[:200]})
                    log.warning("生成 %s/%s 周期 K 线图失败，该图本轮缺失: %s", sym, tf, e)
                    continue
            if not made:
                # 某个币一张图都没有 —— 多币时尤其要能看见「哪个币没图」
                log.warning("vision: %s 一张图都没生成（请求周期 %s）", sym, tfs)
        self._last_chart_meta = meta
        if not out and tfs:
            # vision 开着却一张图都没生成 —— 此前是彻底静默的，等于「不发图但没人知道」
            log.warning("vision 已开启但一张图都没生成（请求周期 %s），本轮将无图发给模型", tfs)
        return out

    def _generate_chart(self, snapshot: dict) -> Optional[str]:
        """单图（主周期）；多周期请用 _generate_charts。"""
        charts = self._generate_charts(snapshot)
        return charts[0] if charts else None
        """从 snapshot 的 candles/indicators 生成 K 线图 base64（vision 模式）。"""
        try:
            from .vision import generate_and_encode
            symbols = self.cfg.symbols or []
            if not symbols:
                return None
            sym = symbols[0]
            market = snapshot.get("market") or {}
            # snapshot: market[sym] = {candles, indicators, ...}
            m = market.get(sym) or {}
            if not isinstance(m, dict):
                m = {}
            klines = (
                m.get("candles")
                or m.get("klines")
                or m.get("kline")
                or market.get("klines")
                or []
            )
            if isinstance(klines, dict):
                klines = klines.get("candles") or klines.get("klines") or []
            if not klines or len(klines) < 10:
                return None
            # 指标：snapshot.indicators → 合并进 kline 字段（vision 自算兜底）
            ind = m.get("indicators") or {}
            merged = []
            for i, k in enumerate(klines):
                row = dict(k) if isinstance(k, dict) else {"c": k}
                for name, series in (ind or {}).items():
                    if isinstance(series, (list, tuple)) and i < len(series):
                        row.setdefault(name, series[i])
                merged.append(row)
            return generate_and_encode(
                merged, symbol=sym, timeframe=self.cfg.timeframe,
            )
        except Exception:  # noqa: BLE001
            return None

    def _chat_with_tools(self, system: str, user: str, chart_base64=None) -> str:
        """工具循环 + **防偷懒**：若全程未调用任何工具，强制重试一次。"""
        text = self._chat_with_tools_inner(system, user, chart_base64=chart_base64)
        tools_on = bool(self.cfg.tools.get("enabled"))
        if tools_on and not self.tool_usage:
            log.warning("no tool calls this cycle; forcing a data-fetch retry")
            nudge = (
                "【强制要求】你上一轮**没有调用任何行情工具**就想输出计划，这是违规的。"
                "现在必须先用工具获取真实市场数据（klines / indicators / ticker / orderbook / "
                "smc_map / smc_events / sqzmom / trades_flow 至少一个），拿到数据后再输出 Plan JSON。"
            )
            text2 = self._chat_with_tools_inner(system, user + "\n\n" + nudge,
                                                chart_base64=chart_base64)
            if self.tool_usage:
                return text2
        return text

    def _chat_with_tools_inner(self, system: str, user: str,
                               chart_base64=None) -> str:
        """Official function-calling loop; falls back to text tool_calls JSON.

        chart_base64: str 或 list[str]，非空时附 K 线图（vision，需模型支持 image）。
        """
        if hasattr(self.llm, "last_reasoning_chain"):
            self.llm.last_reasoning_chain = []
        tools_on = bool(self.cfg.tools.get("enabled"))
        max_rounds = int(self.cfg.tools.get("max_rounds") or 3)
        native = bool(self.cfg.tools.get("native", True))
        # vision：content 用 list 格式（text + 多张 image_url）
        if chart_base64:
            imgs = chart_base64 if isinstance(chart_base64, (list, tuple)) else [chart_base64]
            user_content: Any = [{"type": "text", "text": user}]
            for img in imgs:
                if img:
                    user_content.append({"type": "image_url", "image_url": {"url": img}})
        else:
            user_content = user
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        if not tools_on:
            return self._llm_send(messages)
        if native and hasattr(self.llm, "chat_message_full"):
            return self._chat_native_tools(messages, max_rounds)
        text = self._llm_send(messages)
        for _round in range(max(1, max_rounds)):
            calls = extract_tool_calls(text)
            if not calls:
                return text
            results = []
            for c in calls[:8]:
                name = c.get("tool") or c.get("name") or ""
                args = c.get("args") or c.get("arguments") or {}
                raw_args = dict(args)  # run_tool 会就地补 symbol，审计要的是模型原样
                res = run_tool(
                    self.client, name, args,
                    env=self.cfg.env, bot_root=self.cfg.bot_root, market_cfg=self.cfg.market,
                    bot_id=self.cfg.bot_id, skill_ids=self.cfg.skills,
                    allow=self.cfg.tools.get("allow"), deny=self.cfg.tools.get("deny"),
                    symbols=self.cfg.symbols,
                )
                self._record_tool_use(name, args, res, raw_args=raw_args)
                results.append({"tool": name, "result": res})
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": "【工具结果】\n" + json.dumps(results, ensure_ascii=False)
                + "\n\n若信息足够，请只输出最终 Plan JSON；仍需数据则继续 tool_calls。",
            })
            text = self._llm_send(messages)
        return text

    def _skill_allowed_tools(self, skill_id: str) -> Optional[set]:
        """读取某 skill 声明的 allowed-tools（None = 不收窄）。"""
        try:
            from ..skillkit import SkillRegistry

            root = Path(self.cfg.bot_root) if self.cfg.bot_root else Path.cwd()
            reg = SkillRegistry()
            reg.scan([root / ".mimocode" / "skills", root / "skills"])
            meta = reg.get_meta(skill_id)
            if meta and meta.allowed_tools:
                return set(meta.allowed_tools)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _chat_native_tools(self, messages: list, max_rounds: int) -> str:
        """DeepSeek function calling + thinking: must echo reasoning_content on tool turns.

        allowed-tools：激活带该声明的 skill 后，后续轮次工具面收窄（skill/skill_ref 始终保留）。
        """
        META_TOOLS = {"skill", "skill_ref"}
        # 只挂「实际能用」的工具：pa-data-source 的 aux_cache.db 不在时，那 10 个 aux
        # 工具每次调用都只会返回 not found（实测实盘占累计调用的 7.3%）。见
        # tools.available_native_tools 的说明。
        # 再按 bot 配置收窄（`strategist.tools.allow` / `.deny`，支持通配）——
        # 这是**程序强制**的：模型看不到就调不到，不依赖它自觉遵守提示词。
        base_tools = filter_tool_schemas(
            available_native_tools(self.cfg.bot_root),
            allow=self.cfg.tools.get("allow"),
            deny=self.cfg.tools.get("deny"),
        )
        active_tools: Optional[list] = None
        for _round in range(max(1, max_rounds) + 1):
            tools = active_tools if active_tools is not None else base_tools
            msg = self.llm.chat_message_full(messages, tools=tools, tool_choice="auto")
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or ""
            # official: append full assistant message incl. reasoning_content
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "reasoning_content": msg.get("reasoning_content") or "",
                "tool_calls": calls,
            })
            for tc in calls[:8]:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:  # noqa: BLE001
                    args = {}
                raw_args = dict(args)  # run_tool 会就地补 symbol，审计要的是模型原样
                result = run_tool(
                    self.client, name, args,
                    env=self.cfg.env, bot_root=self.cfg.bot_root, market_cfg=self.cfg.market,
                    bot_id=self.cfg.bot_id, skill_ids=self.cfg.skills,
                    allow=self.cfg.tools.get("allow"), deny=self.cfg.tools.get("deny"),
                    symbols=self.cfg.symbols,
                )
                self._record_tool_use(name, args, result, raw_args=raw_args)
                # 收窄：skill 成功加载且声明 allowed-tools → 限定后续工具面
                if name == "skill" and isinstance(result, dict) and result.get("content"):
                    allowed = self._skill_allowed_tools(str(args.get("name") or ""))
                    if allowed:
                        keep = set(allowed) | META_TOOLS
                        narrowed = [t for t in base_tools if t["function"]["name"] in keep]
                        if narrowed:
                            active_tools = narrowed
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": json.dumps(result, ensure_ascii=False),
                })
        # force final without tools
        messages.append({
            "role": "user",
            "content": "工具结果已足够。禁止再调用工具，禁止使用特殊标记。"
            "请只输出最终 Plan JSON 对象。",
        })
        msg = self.llm.chat_message_full(messages)
        return msg.get("content") or ""

    def _kline_closed(self) -> bool:
        """宇宙内**任一币**的最新 K 线前进即唤醒；单币路径与旧实现逐字一致。

        取数走 fetch_rest_candles（新旧客户端都兼容）。此前直接调
        client.public_get，而客户端重构成 ExchangeClient 后没有该方法 →
        异常被吞、永远返回 False → kline_close 事件从未触发过。

        **为什么必须遍历整个宇宙**：旧实现只取宇宙里的**第一个**币，多币 bot 的第 2..N 个币
        的收盘事件**永远唤不醒**（而 `docs/compose/spec/llm-strategist.md:119` 还把这个
        行为写成了"规格"，于是既没被测试拦住、也没被标成缺陷）。
        某个币取数失败只跳过它自己：不唤醒、也**不把它当成"已观测"** —— 基线记 `0`
        （= 未知），它恢复后的第一根 K 线因此仍能唤醒。若失败那轮干脆不写，恢复时会与
        "全新未观测"混淆，那笔收盘事件就永久丢了。
        """
        if not self.cfg.symbols:
            return False
        interval = self.cfg.event_timeframe or self.cfg.timeframe
        try:
            from .market import fetch_rest_candles
        except Exception:  # noqa: BLE001
            return False
        # 单币沿用历史属性 `_last_kline_t`（既有调用方与测试按 int 读写它）；
        # 多币才需要逐币基线。
        single = len(self.cfg.symbols) == 1
        by_sym = getattr(self, "_last_kline_t_by_symbol", None)
        if by_sym is None:
            by_sym = self._last_kline_t_by_symbol = {}
        closed: list[str] = []

        def _mark_unknown(sym: str) -> None:
            """该币本轮没读到 → 基线记「未知」（0）。

            为什么要写而不是不写：不写的话，它下次成功读取会与"全新未观测"（键不存在）
            混为一谈，于是**这笔收盘事件永久丢失**。记 0 则保证它恢复后仍会唤醒一次。
            单币路径**不动** `_last_kline_t` —— 那是历史行为（I11 逐字不变）。
            """
            if not single:
                by_sym[sym] = 0

        for sym in self.cfg.symbols:
            try:
                rows = fetch_rest_candles(self.client, sym, interval, 2)
            except Exception:  # noqa: BLE001 — 一个币取不到不该拖住其他币
                log.warning("kline_close: %s 取数失败，本轮跳过该币（不影响其他币）", sym)
                _mark_unknown(sym)
                continue
            if not rows:
                _mark_unknown(sym)
                continue
            ts = int((rows[-1] or {}).get("t") or 0)
            if not ts:
                _mark_unknown(sym)
                continue
            if single:
                prev = self._last_kline_t
                self._last_kline_t = ts
            else:
                prev = by_sym.get(sym)
                by_sym[sym] = ts
            if prev is not None and ts > prev:
                closed.append(sym)
        if closed:
            # 保留"上一次唤醒是谁收盘"——`_kline_close_trigger()` 在唤醒后立即被调用，
            # 中途的空判定不该把它抹掉（同一分钟多币收盘只唤醒一次）。
            self._kline_close_symbols = closed
        return bool(closed)

    def _kline_close_trigger(self) -> str:
        """kline_close 的触发标签：单币保持 `kline_close`（逐字不变），多币带出收盘的币。

        带上币名是为了让日志与 cycle 元数据能回答"这轮是谁收盘唤醒的"——
        多币下 `plan[kline_close]` 无法区分，排查时看不出是哪条腿在驱动节奏。
        """
        syms = list(getattr(self, "_kline_close_symbols", None) or [])
        if len(self.cfg.symbols or []) <= 1 or not syms:
            return "kline_close"
        return "kline_close:" + ",".join(syms)

    def _apply_ai_triggers(self, plan) -> list:
        """Apply Plan.triggers / trigger_ops under ai_triggers policy."""
        notes = []
        if not getattr(self, "_ai_policy", None) or not self._ai_policy.enabled:
            return notes
        ops = plan.trigger_ops or []
        for op in ops:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op") or "").lower()
            try:
                if kind == "remove":
                    self._ai_store.remove(str(op.get("id") or ""))
                elif kind == "replace_all":
                    norms = []
                    for raw in plan.triggers or []:
                        norms.append(validate_trigger_payload(raw, self._ai_policy, self.cfg.symbols))
                    self._ai_store.replace_all(norms, self.cfg.symbols)
                    notes.append(f"ai_triggers: replaced {len(norms)}")
            except TriggerPolicyError as e:
                notes.append(f"trigger_op_rejected: {e}")
        if not ops or any(str(op.get("op") or "").lower() == "add" for op in ops if isinstance(op, dict)):
            for raw in plan.triggers or []:
                try:
                    norm = validate_trigger_payload(raw, self._ai_policy, self.cfg.symbols)
                    t = self._ai_store.add(norm, self.cfg.symbols)
                    notes.append(f"ai_trigger_add: {t.id} {t.type} {t.symbol}")
                except TriggerPolicyError as e:
                    notes.append(f"trigger_rejected: {e}")
        return notes

    def _check_condition_events(self) -> Optional[str]:
        ai_conds = []
        try:
            ai_conds = [t.to_condition() for t in self._ai_store.active()] if getattr(self, "_ai_store", None) else []
        except Exception:  # noqa: BLE001
            ai_conds = []
        all_conds = list(self._conditions) + ai_conds
        if not all_conds:
            return None
        interval = self.cfg.event_timeframe or self.cfg.timeframe
        fired = check_conditions(
            self.client, all_conds, interval, states=self._cond_states
        )
        if not fired:
            return None
        reasons = "; ".join(f"{f['type']}:{f['symbol']}:{f['reason']}" for f in fired)
        log.info("condition fired: %s", reasons)
        return f"cond[{reasons}]"

    def run_forever(self) -> None:
        last_interval = 0.0
        while True:
            now = time.time()
            fire = False
            trigger = ""
            if self.cfg.interval_sec and now - last_interval >= self.cfg.interval_sec:
                fire, trigger = True, "interval"
                last_interval = now
            elif self.cfg.event_on_kline_close and self._kline_closed():
                fire, trigger = True, self._kline_close_trigger()
            else:
                cond = self._check_condition_events()
                if cond:
                    fire, trigger = True, cond
            if fire:
                if not self._busy.acquire(blocking=False):
                    continue
                try:
                    result = self.run_once(trigger=trigger)
                    log.info("plan[%s] %s", trigger, result)
                except LLMError as e:
                    log.warning("plan[%s] llm error: %s", trigger, e)
                except Exception as e:  # noqa: BLE001
                    log.exception("plan[%s] failed: %s", trigger, e)
                finally:
                    self._busy.release()
            time.sleep(self.cfg.check_interval_sec or 1.0)
