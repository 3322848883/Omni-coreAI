"""PersonaRunner：触发 → 各人格独立分析 → 融合 → 执行。

拓扑：
  single_account  融合动作 → target_account 单次执行（去重）
  mirror_accounts 融合动作 → 各成员账户同步（复用 broadcast 原子分发）
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from .config import PersonaGroup, PersonaError, member_weight, validate_group
from .fusion import DIR_HOLD, fuse_plans
from .orders import SharedOrderStore, new_order_id


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

        fusion = fuse_plans(self.group, flat)
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
        }

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
        return result

    def _symbol_of(self, plans: dict) -> str:
        for p in plans.values():
            chips = p.get("chips") or []
            if chips and isinstance(chips, list) and chips[0].get("symbol"):
                return str(chips[0]["symbol"])
        return "BTC_USDT"

    def _resolve_order_id(self, decision: str, fusion: dict) -> Optional[str]:
        """订单生命周期：开仓建新单；持仓期（hold/管理动作）复用现有 open 单。"""
        opens = self.orders.list_open()
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
        # 风控：用 source_bot 的 strategist 风险配置（不能绕过）
        payload = self._apply_risk(source_bot, payload)

        if self.group.topology == "single_account":
            return self._exec_single(payload, order_id)
        return self._exec_mirror(payload, order_id)

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
        fname = f"{int(time.time())}-{payload['meta'].get('order_id') or 'po'}.json"
        path = inbox / fname
        tmp = path.with_name(f".{fname}.{os.getpid()}.{time.time_ns()}.writing")
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
                fname = f"{int(time.time())}-{payload['meta'].get('order_id') or 'po'}.json"
                dst = inbox / fname
                if dst.exists():
                    delivered.append({"bot": bot, "status": "exists"})
                    continue
                tmp = dst.with_name(f".{fname}.{os.getpid()}.{time.time_ns()}.writing")
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
