"""PersonaRunner：触发 → 各人格独立分析 → 融合 → 执行。

拓扑：
  single_account  融合动作 → target_account 单次执行（去重）
  mirror_accounts 融合动作 → 各成员账户同步（复用 broadcast 原子分发）
"""
from __future__ import annotations

import json
import logging
import os
import random
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
            # 设计 S2.4 要求**每轮**都进 journal（含 hold）。原先这里直接 return，
            # 于是 hold 轮完全不留痕 —— 而近况摘要与缓存命中曲线都靠 journal，
            # 只在成交时记会让「近况」几乎空白（实测：真实一轮 hold 后 journal 零新增）。
            self._post_exec_hooks(order_id, fusion, flat, result, cycle_id)
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

    @staticmethod
    def _tier1_from_chip(chip: Any) -> dict:
        """契约 Tier 1 字段（docs/compose/spec/pa-skills-upgrade.md [S2]）。

        人格路径的 plan 是**普通 dict**（`analyze_once` 的 chips_out），不是 Chip 对象，
        所以这里按 dict 读。全部可选：没填就不写键。
        """
        out: dict[str, Any] = {}
        if not isinstance(chip, dict):
            return out
        if chip.get("region"):
            out["region"] = chip["region"]
        if chip.get("rule_ids"):
            out["rule_ids"] = list(chip["rule_ids"])
        if chip.get("risk_pct") is not None:
            out["risk_pct"] = chip["risk_pct"]
        if chip.get("invalidation") is not None:
            out["invalidation_price"] = chip["invalidation"]
        # 与 `loop.py::_tier1_journal_fields` 保持同一字段集 —— 两边不一致时
        # 人格路径的 journal 会永远缺这两格（独立评审指出的近重复 helper 漂移）。
        if chip.get("time_stop_bars") is not None:
            out["time_stop_bars"] = chip["time_stop_bars"]
        if chip.get("give_back_pct") is not None:
            out["give_back_pct"] = chip["give_back_pct"]
        return out

    def _post_exec_hooks(self, order_id: Optional[str], fusion: dict,
                         plans: dict, result: dict, cycle_id: str) -> None:
        """每轮收尾：订单上下文（lifecycle/recent_events）+ 追加 journal。

        **journal 不依赖是否有单** —— 设计 S2.4 要求每轮一条（含 hold），
        近况摘要与缓存命中曲线都靠它。只有真有单时才更新订单上下文。
        """
        action = result.get("action") or fusion.get("action") or "hold"
        decision = result.get("decision") or fusion.get("decision") or ""
        executed = bool(result.get("executed"))
        source_bot = fusion.get("master") or ""
        if not source_bot:
            votes = fusion.get("votes") or {}
            source_bot = next(iter(votes), "")

        # lifecycle 管理事件（只有真的有单、且真的执行了才记）
        if order_id and executed:
            act_key = self._lifecycle_act(action)
            self.orders.add_lifecycle(
                order_id, act_key,
                detail=f"{action} {decision}",
                by=f"fusion:{fusion.get('mode', '')}",
            )
            # 开仓：落开仓理由 + 引用本轮 journal 索引（设计 S2.3 / FinPos memory_refs）
            if act_key == "open":
                reason = str((plans.get(source_bot) or {}).get("reasoning")
                             or fusion.get("reason") or "")[:500]
                if reason:
                    self.orders.set_reason(order_id, reason)
                if cycle_id:
                    self.orders.add_memory_ref(order_id, f"journal:{cycle_id}")
                # 契约 Tier 1：把模型声明的前提失效价落到订单上 —— 下一轮它会在
                # [订单上下文] 里看到这个**值**，据此判断「结构坏了没有」。
                chip0 = ((plans.get(source_bot) or {}).get("chips") or [{}])[0]
                if isinstance(chip0, dict) and chip0.get("invalidation") is not None:
                    try:
                        self.orders.set_premise_invalidation(
                            order_id, float(chip0["invalidation"]),
                            note=str(chip0.get("region") or ""))
                    except (TypeError, ValueError, KeyError):
                        pass
            self.orders.refresh_recent_events(order_id)
            # 平仓：更新 profile
            if self._order_status_after(action) == "closed":
                self._update_profile_on_close(order_id)

        # journal 追加（每轮，含 hold；含快照摘要/模型/缓存命中，设计 S2.4）
        try:
            from ..memory import MemoryJournal
            journal = MemoryJournal(self.root, source_bot or self.group.members[0])
            plan = plans.get(source_bot) or {}
            runner = (getattr(self, "plan_runners", None) or {}).get(source_bot)
            llm = getattr(runner, "llm", None)
            hit = int(llm.cache_hit_tokens()) if hasattr(llm, "cache_hit_tokens") else 0
            journal.append(
                cycle_id=cycle_id or f"c-{int(time.time())}",
                decision=decision,
                reasoning=str(plan.get("reasoning") or fusion.get("reason") or "")[:200],
                memory_refs=[f"order:{order_id}"] if order_id else [],
                snapshot_digest=str(getattr(runner, "last_snapshot_digest", "") or ""),
                llm_model=str(getattr(llm, "last_model", "") or ""),
                prompt_cache_hit_tokens=hit,
                executed=executed,
                exec_result={"action": action, "order_id": order_id},
                **self._tier1_from_chip((plan.get("chips") or [{}])[0]),
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

            # 本轮发言顺序。sync 下顺序不影响结果（三人读同一份快照）；
            # relay 下它就是「谁先定调」—— random 每轮重新洗牌，避免固定第一人
            # 长期主导（实测 eth-disc 连续 4 轮都是 SMC 第一个发言且先定方向）。
            order = [b for b in self.group.members
                     if current.get(b) and (current[b] or {}).get("ok", True)]
            if d.mode == "relay" and d.order == "random":
                random.shuffle(order)
            # 轮开始时的立场快照。relay 下 current 会被逐个更新，事后取不到
            # 「原来是什么」，所以必须在发言前存一份给日志用。
            was = {b: str((current.get(b) or {}).get("decision") or "")
                   for b in self.group.members}

            # 构建讨论消息：每人看到其他人的 decision + reasoning
            discussions = {}
            for bot_id in order:
                plan = current.get(bot_id)
                if not plan or not plan.get("ok", True):
                    continue
                # 收集其他人的观点（relay 下 current 已含本轮前面人的最新立场）
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
                except Exception:  # noqa: BLE001
                    continue
                if not revised:
                    continue
                discussions[bot_id] = revised
                if d.mode == "relay":
                    # 接力：说完立刻生效，后面的人看得到它本轮的新立场
                    current[bot_id] = self._apply_revision(current[bot_id], revised)

            # 记录本轮：改口 vs 坚持
            # 阶段名必须跟着**实际轮数**走。原先按固定 round_num 映射
            # （1=相互讨论、2=深化、其余=最终决策），把轮数降到 2 之后最后一轮
            # 会被标成「深化讨论」，而 `StrategyLoop.discuss` 同一轮告诉模型的是
            # 「最终决策」阶段 —— 审计日志与模型实际看到的东西对不上。
            if round_num == 1:
                stage = "相互讨论与反驳"
            elif round_num >= max_rounds:
                stage = "最终决策"
            else:
                stage = "深化讨论"
            round_rec = {"round": round_num, "stage": stage,
                         "mode": d.mode, "order": list(order), "positions": {}}
            for bot_id in self.group.members:
                new = discussions.get(bot_id)
                old_dec = was.get(bot_id, "")
                if new:
                    new_dec = str(new.get("decision") or old_dec)
                    round_rec["positions"][bot_id] = {
                        "was": old_dec,
                        "now": new_dec,
                        "changed": new_dec != old_dec,
                        "reasoning": str(new.get("reasoning") or "")[:80],
                    }
                else:
                    round_rec["positions"][bot_id] = {
                        "was": old_dec, "now": old_dec, "changed": False,
                        "reasoning": "(未修订/保持)",
                    }
            self.discussion_log.append(round_rec)

            if not discussions:
                # 没有有效修正，退出
                return current, round_num

            if d.mode == "sync":
                # 同步轮次：轮末统一生效。轮内 current 保持不变，所以三人读的是同一份
                # 上一轮快照。relay 已在每次发言后应用过，这里不再重复。
                for bot_id, revised in discussions.items():
                    current[bot_id] = self._apply_revision(current[bot_id], revised)

            # 最后一轮不再检查一致（直接融合）
            if round_num == max_rounds:
                return current, max_rounds

        return current, max_rounds

    @staticmethod
    def _apply_revision(base_plan: dict, revised: dict) -> dict:
        """把讨论修订合并进 plan —— **必须连 chips 一起改**。

        `fuse_plans._norm_dir` 与 `_execute` 都优先读 `chips[0].action`，
        只改 `decision` 的话讨论结果进不了融合（线上实测：三人讨论后都改成
        stop_entry_long，融合票仍是讨论前的 hold/long/hold）。
        """
        merged = {**base_plan, **{k: v for k, v in revised.items() if k != "chip"}}
        chip = revised.get("chip")
        if chip is None:
            # `_discussion_chip` 改成 hold（或非入场类）时返回 None。
            # **不能清空 chips**：清空虽然能让 `_norm_dir` 读到新的 `decision`，
            # 却把原始 chip 的 symbol/size_usd/tp/sl 一起丢了 —— 下一轮该人格再改回
            # 入场类时 `_discussion_chip` 的 base 为空，symbol 就落到硬编码兜底。
            # 实测 eth-disc 一轮里三张 chip 全部变成 `BTC_USDT`
            # （讨论的是 ETH，价位也是 ETH 的 2694/2706）。
            # 只改写 action，其余执行字段原样保留。
            keep = list(base_plan.get("chips") or [])
            if keep and isinstance(keep[0], dict):
                keep[0] = {**keep[0], "action": str(merged.get("decision") or "hold")}
            merged["chips"] = keep
        else:
            rest = list((base_plan.get("chips") or [])[1:])
            merged["chips"] = [chip] + rest
        return merged

    def _revise_with_discussion(self, bot_id: str, plan: dict,
                                 peers: list[dict], round_num: int,
                                 max_rounds: int = 3) -> Optional[dict]:
        """让一个 personality 基于讨论修正决策（三阶段递进）。

        阶段：首轮=相互讨论与反驳，末轮=最终决策，中间=深化讨论
        （按**实际轮数**划分，不按固定轮号 —— 见 `_discuss` 里 stage 的注释）
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

    def _exec_facts_of(self, order_id: str, target_account: str) -> dict:
        """从目标账户的成交日志里取这笔单的执行事实：已实现盈亏 + 成交价。

        人格组下单只是把信号写进目标账户的 inbox（`_exec_single`），由那个账户的
        `run` 进程**异步**执行 —— 所以本进程当时根本不知道成交结果，这正是原先
        `record_trade(pnl_usd=0.0)` 的由来。`tradelog.log_execution` 会把
        `signal.meta.order_id` 落进成交日志（见那里的注释），据此回连。

        返回 `{"pnl": float|None, "entry_price": float|None}` —— 取不到就是 None，
        **调用方不能拿 0 顶替**（那会把盈利单记成胜率 0%）。
        """
        facts: dict = {"pnl": None, "entry_price": None}
        # ── 1) 优先读**执行回执**（执行侧主动回传的结构化结果）──
        # 回执按 (order_id, cycle) 存，这里扫目录找该 order_id 最新的一条。
        # 它比扫 trades.jsonl 可靠：那条路径靠「尾 500 行」猜，实测漏记 458/459 笔。
        try:
            rd = self.root / "data" / "shared" / "receipts"
            if rd.exists():
                best: Optional[dict] = None
                for p in rd.glob("*.json"):
                    try:
                        rec = json.loads(p.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        continue
                    if str(rec.get("order_id") or "") != str(order_id):
                        continue
                    if best is None or int(rec.get("ts") or 0) > int(best.get("ts") or 0):
                        best = rec
                if best:
                    if best.get("realized_pnl") is not None:
                        facts["pnl"] = float(best["realized_pnl"])
                    if best.get("entry_price") is not None:
                        facts["entry_price"] = float(best["entry_price"])
                    if facts["pnl"] is not None or facts["entry_price"] is not None:
                        return facts
        except Exception:  # noqa: BLE001
            pass
        try:
            from ..paths import bot_paths
            p = bot_paths(self.root, target_account, create=False).logs / "trades.jsonl"
            if not p.is_file():
                return facts
            # 成交日志是 append-only；我们关心的是这笔单，扫尾部足够
            lines = p.read_text(encoding="utf-8").splitlines()[-500:]
        except Exception:  # noqa: BLE001
            return facts
        for ln in lines:
            try:
                rec = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if str(rec.get("order_id") or "") != str(order_id):
                continue
            for st in (rec.get("steps") or []):
                d = st.get("detail") or {}
                if d.get("realized_pnl") is not None:
                    try:
                        facts["pnl"] = float(d["realized_pnl"])  # 同单多次平仓 → 取最后一次
                    except (TypeError, ValueError):
                        pass
                # 入场价只从**开仓类**步骤取（平仓步骤的 entry_price 实为平仓成交价）
                act = str(st.get("action") or "")
                if facts["entry_price"] is None and act.startswith(("open", "stop_entry", "add")):
                    px = d.get("entry_price") or d.get("fill_price") or d.get("avg_price")
                    try:
                        facts["entry_price"] = float(px)
                    except (TypeError, ValueError):
                        pass
        return facts

    def _update_profile_on_close(self, order_id: str) -> None:
        """平仓后更新策略画像（确定性统计）。

        **不再写死 `pnl_usd=0.0`** —— 那会把盈利单记成「胜率 0%、均盈亏 0u」，
        而 `MemoryProfile.prompt_summary()` 会把它拼进 system prompt，
        等于告诉 AI「你的策略一直在输」。比没有数据更糟。

        真实盈亏从成交日志回连（`_exec_facts_of`）；**取不到就跳过** ——
        宁可不记这一笔，也不记一条假的（信号可能还没被执行：目标账户没起 `run`）。
        """
        try:
            from ..memory import MemoryProfile
            rec = self.orders.get(order_id)
            if rec is None:
                return
            members = list(self.group.members or [])
            target = str(rec.get("target_account") or (members[0] if members else ""))
            facts = self._exec_facts_of(order_id, target)
            # 成交价一旦拿到就补进订单上下文（设计 S2.3 的 `entry_price`）——
            # 开仓时成交结果未知，只能在这里回填；取不到就保持 None。
            if facts.get("entry_price") is not None:
                try:
                    self.orders.update(order_id, entry_price=facts["entry_price"])
                except Exception:  # noqa: BLE001
                    pass
            pnl = facts.get("pnl")
            if pnl is None:
                self._log({"event": "profile_skip", "order_id": order_id,
                           "target_account": target,
                           "reason": "no realized_pnl yet（信号可能还没被执行）"})
                return
            # 画像按**持有仓位的账户**（target）建 —— paper 账本在它名下，
            # `MemoryProfile.load()` 的账本投影也按 bot_id 去找库。
            # 原先用 members[0]：组内成员与目标账户不一致时，画像会挂到一个
            # 账本为空的 bot 上，投影直接失效。
            profile = MemoryProfile(self.root, target or (members[0] if members else ""))
            lifecycle = rec.get("lifecycle") or []
            hold_rounds = len([e for e in lifecycle if e.get("act") != "close"])
            # 归类用**开仓动作**（lifecycle 首条的 detail 形如 "open_long long" /
            # "stop_entry_short short"，取第一个词）—— 设计 S2.5 的 best_act/worst_act
            # 要按动作看哪类进场更赚。
            act = ""
            for e in lifecycle:
                if e.get("act") == "open":
                    act = str(e.get("detail") or "").split(" ")[0]
                    break
            profile.record_trade(pnl_usd=pnl, hold_rounds=hold_rounds, action=act,
                                 entry_price=float(facts.get("entry_price") or 0))
        except Exception:  # noqa: BLE001
            pass

    def _symbols_of(self, bot_id: str) -> list[str]:
        """某成员配置里的标的白名单 —— 讨论 chip 缺/错 symbol 时的纠正依据。"""
        bot = self.bots.get(bot_id)
        syms = getattr(bot, "symbols", None) if bot is not None else None
        return [str(s) for s in (syms or []) if s]

    def _symbol_of(self, plans: dict) -> str:
        for p in plans.values():
            chips = p.get("chips") or []
            if chips and isinstance(chips, list) and chips[0].get("symbol"):
                return str(chips[0]["symbol"])
        # 兜底用第一个成员自己的标的 —— 原先是硬编码 "BTC_USDT"，
        # 对只做 ETH 的组会把共享订单的 symbol 写错。
        for b in self.group.members:
            syms = self._symbols_of(b)
            if syms:
                return syms[0]
        return "BTC_USDT"

    def _resolve_order_id(self, decision: str, fusion: dict) -> Optional[str]:
        """订单生命周期：开仓建新单；持仓期（hold/管理动作）复用现有 open 单。

        按 group 过滤，多组隔离互不干扰。
        """
        opens = self.orders.list_open(group=self.group.name)
        # 方向反转 → **另起一张单**，而不是在旧记录上再记一次 open。
        # 否则注入 prompt 的订单上下文会自相矛盾：实测账户已是 +177 多仓，
        # 而订单上下文还写着「方向: short / 理由: …限价空」（side 与 reason 都是旧的）
        # —— 把持仓方向说反，比没有记忆更危险。
        if opens and decision in ("long", "short"):
            cur = str(opens[0].get("side") or "")
            if cur and cur != decision:
                self.orders.update(opens[0]["order_id"], status="closed")
                return new_order_id()
            return opens[0]["order_id"]
        # 管理动作 / hold：沿用现有 open 单（共同记忆贯穿持仓期）
        if decision in ("hold", "close", "reduce", "modify"):
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

        # chip 的标的必须落在该 bot 自己的白名单里。讨论丢字段后重建的 chip 会带
        # 硬编码兜底（见 `_discuss`）；executor 的白名单闸门本来也会把这种信号整笔
        # 拒掉，但那样只留下一条 failed —— 不如在这里纠正成该 bot 自己的标的并留痕。
        sym = str(chip.get("symbol") or "").strip()
        allowed = self._symbols_of(source_bot)
        corrected_from = ""
        if allowed and sym not in allowed:
            corrected_from = sym
            sym = allowed[0]
        if not sym:
            sym = allowed[0] if allowed else "BTC_USDT"

        payload = {
            "action": action,
            "symbol": sym,
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
        if corrected_from:
            payload["meta"]["symbol_corrected"] = f"{corrected_from}→{sym}"
        if side_override:
            payload["side"] = side_override
        # 执行机制字段的透传 —— 尤其 `trigger_price`：没有它，stop_entry_*
        # 会被 executor 以「requires trigger_price」整笔拒掉，人格的突破单从来没挂出去过。
        #
        # **只透传机制，不透传风险**：`size`（张数）与 `leverage` 是风险项 ——
        # `_apply_risk` 只钳 `size_usd`，把 LLM 的 `size`/`leverage` 也放过去
        # 就等于让 Plan 绕过 yaml 风控（实测：LLM 提了 leverage=50，而配置是 20，
        # pt-b1 又没配 account_risk.max_leverage，闸门不生效）。
        for k in ("price", "trigger_price", "trigger_price_type"):
            if chip.get(k) is not None:
                payload[k] = chip.get(k)
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

    def _note_invalidation(self, order_id: Optional[str], payload: dict) -> None:
        """tp/sl 被改动时把旧价位标记为「假设失效」（设计 S2.7，Memora FAMA）。

        触发点选「管理动作改了 tp/sl」：旧止盈止损代表的是**旧的行情假设**，
        被改掉即说明那个假设不再成立 —— 这是 invalidation 唯一能自动判定的时刻。
        """
        if not order_id:
            return
        try:
            rec = self.orders.get(order_id) or {}
            for field in ("tp", "sl"):
                old, new = rec.get(field), payload.get(field)
                if old is None or new is None:
                    continue
                if float(old) == float(new):
                    continue
                self.orders.add_invalidation(order_id, field, old, new)
        except Exception:  # noqa: BLE001
            pass

    def _exec_single(self, payload: dict, order_id: Optional[str]) -> dict:
        """单账户：写 target_account 的 inbox，去重单执行。"""
        self._note_invalidation(order_id, payload)
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
                **{k: payload.get(k) for k in ("symbol", "tp", "sl", "size_usd") if payload.get(k)},
            )
        return {"executed": True, "topology": "single_account",
                "target_account": target, "signal_file": str(path)}

    def _exec_mirror(self, payload: dict, order_id: Optional[str]) -> dict:
        """镜像：广播到各成员账户（原子写 + 校验）。"""
        self._note_invalidation(order_id, payload)
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
                               **{k: payload.get(k) for k in ("symbol", "tp", "sl", "size_usd") if payload.get(k)})
        return {"executed": not failed, "topology": "mirror_accounts",
                "delivered": delivered, "failed": failed}
