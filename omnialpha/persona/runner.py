"""PersonaRunner：触发 → 各人格独立分析 → 融合 → 执行。

拓扑：
  single_account  融合动作 → target_account 单次执行（去重）
  mirror_accounts 融合动作 → 各成员账户同步（复用 broadcast 原子分发）
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from .config import PersonaGroup, PersonaError, member_weight, validate_group
from .fusion import DIR_HOLD, fuse_plans
from .orders import SharedOrderStore, new_order_id

log = logging.getLogger(__name__)


class PersonaRunner:
    def __init__(self, root: Path, group: PersonaGroup, bots: dict, plan_runners: dict):
        """
        bots: {bot_id: BotConfig}
        plan_runners: {bot_id: PlanRunner} —— 各人格的独立分析器
        """
        self.root = Path(root)
        self.group = group
        self.bots = bots
        self.plan_runners = plan_runners
        self.orders = SharedOrderStore(self.root)
        self.log_path = self.root / "data" / "shared" / "persona_log.jsonl"
        self.discussion_log: list = []
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, rec: dict) -> None:
        rec = dict(rec, ts=int(time.time()), group=self.group.name)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ── 分析：各人格独立出 Plan ──────────────────────────
    def analyze_all(self, trigger: str = "manual") -> dict[str, dict]:
        plans: dict[str, dict] = {}
        for bot_id in self.group.members:
            runner = self.plan_runners.get(bot_id)
            if runner is None:
                plans[bot_id] = {"ok": False, "error": "no runner"}
                continue
            try:
                plans[bot_id] = runner.analyze_once(trigger=trigger)
            except Exception as e:  # noqa: BLE001
                # 不丢票：异常也降级为 hold（保留该成员的投票权）
                log.warning("analyze_once raised for %s: %s; degrade to hold", bot_id, e)
                fb = getattr(runner, "_hold_fallback", None)
                if callable(fb):
                    try:
                        plans[bot_id] = fb("", trigger, f"exception: {e}")
                    except Exception:  # noqa: BLE001
                        plans[bot_id] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                else:
                    plans[bot_id] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return plans

    # ── 融合 → 执行 ─────────────────────────────────────
    def run_once(self, trigger: str = "manual") -> dict:
        plans = self.analyze_all(trigger)
        flat = {b: (p.get("plan") or {}) for b, p in plans.items() if p.get("ok")}
        if not flat:
            rec = {"event": "analyze_failed", "plans": plans}
            self._log(rec)
            return {"ok": False, "error": "all_analyze_failed", "plans": plans}

        # 讨论模式：互相看 reasoning → 修正决策（可选，硬预算防无限讨论）
        discussion_rounds_used = 0
        if self.group.discussion.enabled:
            flat, discussion_rounds_used = self._discuss(flat)

        fusion = fuse_plans(self.group, flat)
        if discussion_rounds_used:
            fusion["discussion_rounds"] = discussion_rounds_used
        decision = fusion.get("decision") or DIR_HOLD
        action = fusion.get("action") or "hold"
        cycle_id = next(iter(flat.values())).get("cycle_id") or ""

        # 记录共同记忆：本轮投票（新单先建，再记票）
        order_id = self._resolve_order_id(decision, fusion)
        if order_id:
            if self.orders.get(order_id) is None:
                # 新建共享订单（开仓/首轮）
                try:
                    self.orders.create({
                        "order_id": order_id,
                        "symbol": self._symbol_of(flat),
                        "group": self.group.name,
                        "topology": self.group.topology,
                        "target_account": self.group.target_account or self.group.members[0],
                        "members": list(self.group.members),
                        "side": decision if decision in ("long", "short") else "",
                        "status": "open",
                    })
                except Exception:  # noqa: BLE001
                    pass
            for bot_id, plan in flat.items():
                try:
                    self.orders.record_vote(
                        order_id, cycle_id or f"c-{int(time.time())}", bot_id,
                        decision=str((plan.get("decision") or "hold")),
                        reasoning=str(plan.get("reasoning") or "")[:200],
                        confidence=float(plan.get("confidence") or 0),
                    )
                except Exception:  # noqa: BLE001
                    pass
            self.orders.append_log(order_id, "fusion_decision",
                                   f"{fusion.get('mode')}→{decision} {fusion.get('reason','')}")

        result = {
            "ok": True, "decision": decision, "action": action, "fusion": fusion,
            "plans": {b: {"ok": p.get("ok"), "decision": (p.get("plan") or {}).get("decision")}
                      for b, p in plans.items()},
            "order_id": order_id,
            "discussion_log": self.discussion_log,
        }
        # 讨论过程落盘（可审计辩论细节）
        if self.discussion_log:
            try:
                import json as _json
                import time as _time
                dpath = self.root / "data" / "shared" / "discussion_log.jsonl"
                with dpath.open("a", encoding="utf-8") as fh:
                    fh.write(_json.dumps({
                        "ts": _time.time(), "group": self.group.name,
                        "rounds": len(self.discussion_log) - 1,
                        "log": self.discussion_log,
                    }, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass

        # 仅 hold 不执行；管理动作（close/reduce/modify）与开仓都要执行
        if decision == DIR_HOLD and action in ("hold", ""):
            result["executed"] = False
            self._log({"event": "hold", "fusion": fusion})
            return result

        # 执行映射
        try:
            exec_res = self._execute(fusion, flat, order_id)
            result.update(exec_res)
        except Exception as e:  # noqa: BLE001
            result.update({"ok": False, "error": f"exec: {e}"})
        self._log({"event": "decision", "decision": decision, "fusion": fusion,
                   "order_id": order_id, "exec": result.get("executed")})
        # agent-memory 挂钩：lifecycle + journal
        self._post_exec_hooks(order_id, fusion, flat, result, cycle_id)
        return result

    def _post_exec_hooks(self, order_id: Optional[str], fusion: dict,
                         plans: dict, result: dict, cycle_id: str) -> None:
        """执行后更新订单上下文（lifecycle/recent_events）+ 追加 journal。"""
        if not order_id:
            return
        action = result.get("action") or fusion.get("action") or "hold"
        decision = result.get("decision") or fusion.get("decision") or ""
        executed = bool(result.get("executed"))
        source_bot = fusion.get("master") or ""
        if not source_bot:
            votes = fusion.get("votes") or {}
            source_bot = next(iter(votes), "")

        # lifecycle 管理事件
        if executed:
            act_key = self._lifecycle_act(action)
            self.orders.add_lifecycle(
                order_id, act_key,
                detail=f"{action} {decision}",
                by=f"fusion:{fusion.get('mode', '')}",
            )
            self.orders.refresh_recent_events(order_id)
            # 平仓：更新 profile
            if self._order_status_after(action) == "closed":
                self._update_profile_on_close(order_id)

        # journal 追加（每轮，含 hold）
        try:
            from ..memory import MemoryJournal
            journal = MemoryJournal(self.root, source_bot or self.group.members[0])
            plan = plans.get(source_bot) or {}
            journal.append(
                cycle_id=cycle_id or f"c-{int(time.time())}",
                decision=decision,
                reasoning=str(plan.get("reasoning") or fusion.get("reason") or "")[:200],
                memory_refs=[f"order:{order_id}"],
                executed=executed,
                exec_result={"action": action, "order_id": order_id},
            )
        except Exception:  # noqa: BLE001
            pass

    def _discuss(self, plans: dict[str, dict]) -> tuple[dict[str, dict], int]:
        """讨论模式：互相看 reasoning → 修正决策。

        基于 Du et al. 2023（2-4 轮最优）+ AutoGen 硬预算模式：
        - 固定最大轮次（默认 2，上限 4），绝不无限讨论
        - 首轮全体一致则提前终止（early_exit）
        - 每轮只交换 decision + reasoning（≤50字），不暴露完整 Plan

        返回 (修正后的 plans, 实际轮数)。
        """
        d = self.group.discussion
        max_rounds = min(d.rounds, 4)
        current = dict(plans)
        # 讨论过程记录（每轮：谁说了什么、是否改口）
        self.discussion_log = [{
            "round": 0,
            "stage": "独立分析",
            "positions": {
                b: {"decision": p.get("decision"), "reasoning": str(p.get("reasoning") or "")[:80]}
                for b, p in current.items() if p.get("ok", True)
            },
        }]

        for round_num in range(1, max_rounds + 1):
            # 检查是否全体一致（提前终止）
            decisions = {
                b: str((p.get("decision") or "")).lower()
                for b, p in current.items()
            }
            if d.early_exit_on_agreement and round_num > 1:
                if len(set(decisions.values())) == 1:
                    self.discussion_log.append({
                        "round": round_num, "stage": "一致提前退出",
                        "positions": {b: {"decision": v} for b, v in decisions.items()},
                    })
                    return current, round_num - 1

            # 构建讨论消息：每人看到其他人的 decision + reasoning
            discussions = {}
            for bot_id in self.group.members:
                plan = current.get(bot_id)
                if not plan or not plan.get("ok", True):
                    continue
                # 收集其他人的观点
                peers = []
                for other_id, other_plan in current.items():
                    if other_id == bot_id:
                        continue
                    peers.append({
                        "bot": other_id,
                        "decision": other_plan.get("decision", "hold"),
                        "reasoning": str(other_plan.get("reasoning") or "")[:50],
                    })
                # 让该人格重新审视（修正决策）
                try:
                    revised = self._revise_with_discussion(
                        bot_id, plan, peers, round_num, max_rounds=max_rounds,
                    )
                    if revised:
                        discussions[bot_id] = revised
                except Exception:  # noqa: BLE001
                    continue

            # 记录本轮：改口 vs 坚持
            stage = {1: "相互讨论与反驳", 2: "深化讨论"}.get(round_num, "最终决策")
            round_rec = {"round": round_num, "stage": stage, "positions": {}}
            for bot_id in self.group.members:
                old = current.get(bot_id) or {}
                new = discussions.get(bot_id)
                old_dec = str(old.get("decision") or "")
                if new:
                    new_dec = str(new.get("decision") or old_dec)
                    changed = new_dec != old_dec
                    round_rec["positions"][bot_id] = {
                        "was": old_dec,
                        "now": new_dec,
                        "changed": changed,
                        "reasoning": str(new.get("reasoning") or "")[:80],
                    }
                else:
                    round_rec["positions"][bot_id] = {
                        "was": old_dec, "now": old_dec, "changed": False,
                        "reasoning": "(未修订/保持)",
                    }
            self.discussion_log.append(round_rec)

            # 应用修正 —— **必须连 chips 一起改**。
            # fuse_plans._norm_dir 与 _execute 都优先读 `chips[0].action`，
            # 只改 `decision` 的话讨论结果进不了融合（线上实测：三人讨论后都改成
            # stop_entry_long，融合票仍是讨论前的 hold/long/hold）。
            if discussions:
                for bot_id, revised in discussions.items():
                    merged = {**current[bot_id],
                              **{k: v for k, v in revised.items() if k != "chip"}}
                    chip = revised.get("chip")
                    if chip is None:
                        merged["chips"] = []
                    else:
                        rest = list((current[bot_id].get("chips") or [])[1:])
                        merged["chips"] = [chip] + rest
                    current[bot_id] = merged
            else:
                # 没有有效修正，退出
                return current, round_num

            # 最后一轮不再检查一致（直接融合）
            if round_num == max_rounds:
                return current, max_rounds

        return current, max_rounds

    def _revise_with_discussion(self, bot_id: str, plan: dict,
                                 peers: list[dict], round_num: int,
                                 max_rounds: int = 3) -> Optional[dict]:
        """让一个 personality 基于讨论修正决策（三阶段递进）。

        阶段：1=相互讨论与反驳  2=深化讨论  3=最终决策
        通过 plan_runners 的 LLM 做修正。如果 runner 不支持则返回 None。
        """
        runner = self.plan_runners.get(bot_id)
        if runner is None:
            return None
        # 构建讨论 prompt（简洁，只交换 decision+reasoning）
        peer_lines = [
            f"- {p['bot']}: {p['decision']} — {p['reasoning']}"
            for p in peers
        ]
        discussion_text = "\n".join(peer_lines)

        # 尝试用 runner 的 LLM 做修正（如果支持 discuss 方法）
        if hasattr(runner, "discuss"):
            return runner.discuss(
                plan=plan, peers=peers, round_num=round_num,
                discussion_text=discussion_text, max_rounds=max_rounds,
            )
        # fallback: 不支持讨论则保持原判
        return None

    @staticmethod
    def _lifecycle_act(action: str) -> str:
        """映射执行动作到 lifecycle 事件类型。"""
        a = (action or "").lower()
        if a in ("open_long", "open_short", "add_long", "add_short",
                 "stop_entry_long", "stop_entry_short", "buy_stop", "sell_stop"):
            return "open"
        if a in ("close", "close_all", "flatten", "close_long", "close_short"):
            return "close"
        if a in ("reduce_long", "reduce_short", "reduce"):
            return "reduce"
        if a == "modify_tp_sl":
            return "modify_tp"
        return "hold"

    def _update_profile_on_close(self, order_id: str) -> None:
        """平仓后更新策略画像（确定性统计）。"""
        try:
            from ..memory import MemoryProfile
            rec = self.orders.get(order_id)
            if rec is None:
                return
            profile = MemoryProfile(self.root, self.group.members[0])
            lifecycle = rec.get("lifecycle") or []
            hold_rounds = len([e for e in lifecycle if e.get("act") != "close"])
            profile.record_trade(pnl_usd=0.0, hold_rounds=hold_rounds)
        except Exception:  # noqa: BLE001
            pass

    def _symbol_of(self, plans: dict) -> str:
        for p in plans.values():
            chips = p.get("chips") or []
            if chips and isinstance(chips, list) and chips[0].get("symbol"):
                return str(chips[0]["symbol"])
        return "BTC_USDT"

    def _resolve_order_id(self, decision: str, fusion: dict) -> Optional[str]:
        """订单生命周期：开仓建新单；持仓期（hold/管理动作）复用现有 open 单。

        按 group 过滤，多组隔离互不干扰。
        """
        opens = self.orders.list_open(group=self.group.name)
        # 管理动作 / hold：沿用现有 open 单（共同记忆贯穿持仓期）
        if decision in ("hold", "close", "reduce", "modify") or opens:
            return opens[0]["order_id"] if opens else None
        # 无持仓 + 方向开仓 → 建新单
        if decision in ("long", "short"):
            return new_order_id()
        return opens[0]["order_id"] if opens else None

    def _execute(self, fusion: dict, plans: dict, order_id: Optional[str]) -> dict:
        """按拓扑执行融合动作。"""
        decision = fusion["decision"]
        # 取多数派/裁决者的第一个 chip 作为执行模板
        votes = fusion.get("votes") or {}
        source_bot = self.group.fusion_config.get("master") if fusion.get("mode") == "master_arbiter" else None
        if not source_bot:
            # 取与决策一致的第一个成员
            for b, d in votes.items():
                if d == decision:
                    source_bot = b
                    break
        source_bot = source_bot or self.group.members[0]
        plan = plans.get(source_bot) or {}
        chips = plan.get("chips") or []
        chip = next((c for c in chips if c.get("action")), {})
        action = chip.get("action") or ""
        # fusion.action 是权威执行动作（含 close/reduce/modify）
        action = fusion.get("action") or ""
        if action in ("hold", ""):
            if decision == "long":
                action = "open_long"
            elif decision == "short":
                action = "open_short"
            else:
                action = decision or "hold"

        # 归一化为 schema 合法 action（close_long/close_short 不在 ACTIONS，需拆成 close+side）
        action, side_override = self._normalize_action(action)

        payload = {
            "action": action,
            "symbol": chip.get("symbol") or "BTC_USDT",
            "size_usd": chip.get("size_usd"),
            "tp": chip.get("tp"),
            "sl": chip.get("sl"),
            "type": chip.get("type") or "market",
            "label": f"persona-{self.group.name}",
            "meta": {
                "group": self.group.name, "order_id": order_id,
                "fusion": fusion.get("mode"), "decision": decision,
                "source_bot": source_bot, "votes": fusion.get("votes"),
                "reason": fusion.get("reason"),
                "confidence": fusion.get("confidence") or 0.0,
            },
        }
        if side_override:
            payload["side"] = side_override
        # modify_tp_sl 至少要有一个目标价：两个都没有就退回 hold，
        # 否则 executor 必然报 "requires tp and/or sl"、白烧一轮
        if action == "modify_tp_sl" and payload.get("tp") is None and payload.get("sl") is None:
            payload["action"] = "hold"
            payload.setdefault("meta", {})["modify_skipped"] = "no tp/sl provided"
        # 风控：用 source_bot 的 strategist 风险配置（不能绕过）
        payload = self._apply_risk(source_bot, payload)

        if self.group.topology == "single_account":
            return self._exec_single(payload, order_id)
        return self._exec_mirror(payload, order_id)

    @staticmethod
    def _normalize_action(action: str) -> tuple[str, Optional[str]]:
        """将 persona 决策动作归一化为 schema 合法 action。

        close_long   → close + side=long
        close_short  → close + side=short
        modify_tp_sl → modify_tp_sl（**原样透传**）

        注：`modify_tp_sl` 一直是 schema 合法动作（`schema.ACTIONS` 里有，
        `executor` 会分发给 `_modify_tp_sl`），此前把它降级成 `hold` 是错的 ——
        人格想调整止盈止损时会**静默什么都不做**。payload 里本就带 `tp`/`sl`。
        返回 (schema_action, side_override|None)。
        """
        a = (action or "").lower()
        if a == "close_long":
            return "close", "long"
        if a == "close_short":
            return "close", "short"
        if a in ("modify_tp_sl", "modify", "modify_tp"):
            return "modify_tp_sl", None
        return action, None

    @staticmethod
    def _order_status_after(action: str) -> str:
        a = (action or "").lower()
        if a in ("close", "close_all", "flatten", "close_long", "close_short"):
            return "closed"
        return "open"

    def _apply_risk(self, source_bot: str, payload: dict) -> dict:
        """融合后信号过 source_bot 的 strategist 风控（min_confidence/max_notional）。

        风控只拦**开仓类**动作；close/reduce/modify 是减险出场，不得拦。
        """
        bot = self.bots.get(source_bot)
        risk = ((getattr(bot, "strategist", None) or {}).get("risk") or {}) if bot else {}
        if not risk:
            return payload
        action = str(payload.get("action") or "").lower()
        # 减险出场：跳过 min_confidence / size 抬闸
        is_manage = action in (
            "close", "close_all", "flatten", "close_long", "close_short",
            "reduce_long", "reduce_short", "modify_tp_sl",
        )
        if is_manage:
            return payload
        min_conf = float(risk.get("min_confidence") or 0.0)
        max_notional = risk.get("max_notional_usd")
        conf = float((payload.get("meta") or {}).get("confidence") or 0.0)
        if min_conf and conf < min_conf:
            payload = dict(payload)
            payload["action"] = "hold"
            payload.setdefault("meta", {})["risk_reject"] = f"confidence {conf} < {min_conf}"
            return payload
        if max_notional is not None and payload.get("size_usd") is not None:
            try:
                if float(payload["size_usd"]) > float(max_notional):
                    payload = dict(payload)
                    payload["size_usd"] = float(max_notional)
                    payload.setdefault("meta", {})["risk_capped"] = True
            except (TypeError, ValueError):
                pass
        return payload

    def _exec_single(self, payload: dict, order_id: Optional[str]) -> dict:
        """单账户：写 target_account 的 inbox，去重单执行。"""
        target = self.group.target_account
        from ..paths import BotPaths
        inbox = BotPaths(self.root, target).inbox
        inbox.mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time())}-{payload['meta'].get('order_id') or 'po'}-{os.getpid()}-{time.time_ns()}.json"
        path = inbox / fname
        tmp = path.with_name(f".{fname}.writing")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        if order_id:
            self.orders.update(
                order_id,
                status=self._order_status_after(payload.get("action", "")),
                **{k: payload.get(k) for k in ("symbol", "tp", "sl") if payload.get(k)},
            )
        return {"executed": True, "topology": "single_account",
                "target_account": target, "signal_file": str(path)}

    def _exec_mirror(self, payload: dict, order_id: Optional[str]) -> dict:
        """镜像：广播到各成员账户（原子写 + 校验）。"""
        from ..paths import BotPaths
        delivered, failed = [], []
        for bot in self.group.members:
            try:
                inbox = BotPaths(self.root, bot).inbox
                inbox.mkdir(parents=True, exist_ok=True)
                fname = f"{int(time.time())}-{payload['meta'].get('order_id') or 'po'}-{os.getpid()}-{time.time_ns()}.json"
                dst = inbox / fname
                if dst.exists():
                    delivered.append({"bot": bot, "status": "exists"})
                    continue
                tmp = dst.with_name(f".{fname}.writing")
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, dst)
                json.loads(dst.read_text(encoding="utf-8"))
                delivered.append({"bot": bot, "status": "ok"})
            except Exception as e:  # noqa: BLE001
                failed.append({"bot": bot, "error": f"{type(e).__name__}: {e}"})
        if order_id:
            self.orders.update(order_id, status="open" if "close" not in payload["action"] else "closed",
                               **{k: payload.get(k) for k in ("symbol", "tp", "sl") if payload.get(k)})
        return {"executed": not failed, "topology": "mirror_accounts",
                "delivered": delivered, "failed": failed}
