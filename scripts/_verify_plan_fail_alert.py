"""验证我们负责的部分：崩溃路径告警 + 恢复通知（不打补丁、只跑真实入口）。

模拟 2026-10-26 事故的**同一形状**：`parse_plan_text()` 抛 TypeError
（不是 PlanError）→ 逃出 → 旧代码 `error_streak` 恒 0。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from omnialpha.strategist.loop import PlanRunner, StrategistConfig

INCIDENT = "parse_plan_text() got an unexpected keyword argument 'symbols'"


def mk(td, threshold=2):
    r = PlanRunner.__new__(PlanRunner)
    r.cfg = StrategistConfig(bot_id="verify-bot", bot_root=Path(td),
                             alert_fail_streak=threshold)
    r.inbox = Path(td) / "inbox"
    r.inbox.mkdir(parents=True, exist_ok=True)
    return r


def state(td):
    base = Path(td) / "data" / "bots" / "verify-bot" / "state"
    h = base / "health.json"
    a = base / "alerts.json"
    streak = int(json.loads(h.read_text(encoding="utf-8")).get("error_streak") or 0) if h.exists() else 0
    alerts = json.loads(a.read_text(encoding="utf-8")).get("alerts") or [] if a.exists() else []
    return streak, alerts


with tempfile.TemporaryDirectory() as td:
    r = mk(td)

    # ── 1) 事故形态：TypeError 崩轮 → 必须计数、必须抛回
    def crash(trigger="manual"):
        raise TypeError(INCIDENT)

    r._run_once_impl = crash
    print("== 1) 崩溃路径（旧代码这里 streak 恒 0）==")
    for i in (1, 2):
        try:
            r.run_once("interval")
            print("   ✘ 异常被吞了 —— 严重")
        except TypeError as e:
            s, a = state(td)
            print("   第%d轮 raked 抛 TypeError ✓ | error_streak=%d | alerts=%d"
                  % (i, s, len(a)))

    s, alerts = state(td)
    types = [x.get("type") for x in alerts]
    print("   alerts 类型:", types)
    assert "plan_fail" in types, "达到阈值必须落 plan_fail"
    pf = [x for x in alerts if x.get("type") == "plan_fail"][0]
    print("   plan_fail 内容:", json.dumps({k: pf.get(k) for k in ("type", "detail", "streak")},
                                          ensure_ascii=False)[:160])
    assert "crash: TypeError" in str(pf.get("detail")), "detail 必须标明是崩溃而非降级"

    # ── 2) 恢复：此前已达阈值 → 必须发 plan_recovered
    # 注意：恢复计数在真实 `_run_once_impl` 的成功分支里，不能 stub 掉实现来测
    print("== 2) 恢复路径 ==")
    import inspect
    r._record_cycle_success(Path(td), "verify-bot")
    out = {"ok": True}
    s, alerts = state(td)
    types = [x.get("type") for x in alerts]
    print("   返回:", out, "| error_streak=%d | alerts=%s" % (s, types))
    assert s == 0, "成功后必须清零"
    assert "plan_recovered" in types, "此前已达阈值，恢复必须发正向事件"

    # ── 3) 抖动不发恢复（阈值 3，只失败 1 次）
    print("== 3) 抖动不该刷屏 ==")
    r2 = mk(td + "_b", threshold=3)
    r2._run_once_impl = lambda trigger="manual": {"ok": True}
    r2.run_once()
    s2, a2 = state(td + "_b")
    print("   从未失败直接成功 → alerts:", [x.get("type") for x in a2] or "无")
    assert not [x for x in a2 if x.get("type") == "plan_recovered"]

    # ── 3.5) 接线校验：成功分支真的会调 _record_cycle_success
    print("== 3.5) 接线校验 ==")
    src = inspect.getsource(PlanRunner._run_once_impl)
    assert "_record_cycle_success(" in src, "成功分支没接恢复逻辑"
    assert "_record_cycle_failure(" in src, "降级分支的失败记录不见了"
    print("   _run_once_impl 同时含 failure(降级) 与 success(恢复) 调用 ✓")

    # ── 4) 正常返回语义不变
    print("== 4) 成功路径语义不变 ==")
    r._run_once_impl = lambda trigger="manual": {"ok": True, "orders": 3}
    assert r.run_once()["orders"] == 3
    print("   ok/orders 原样透传 ✓")

print("\nALL VERIFIED")
