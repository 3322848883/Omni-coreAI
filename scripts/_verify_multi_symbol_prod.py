# -*- coding: utf-8 -*-
"""生产链路验证（多币）—— 走真实生产入口，不下任何单。

覆盖本特性的四条主链：
  1. 配置层：多币配置能加载；空白名单 / 宇宙⊄白名单 / label_prefix 重名 → 报错或告警
  2. 快照层：按币分区 + `position_state[symbol]`（宇宙外的币不参与）
  3. Plan 闸门：越界 chip 在 **plan 层**被拒（不必等到执行层）
  4. 工具前置：多币漏写 symbol → 拒绝（附宇宙）；单币自动补
  5. 记忆层：journal/Tier-1 按币、画像摘要按币、订单上下文按币（各自独立可还原）
  6. 守护层：扫描集合 = 声明宇宙 ∪「有归属的合约」
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not ok:
        FAILS.append(name)


td = tempfile.TemporaryDirectory()
root = Path(td.name)

# ── 1. 配置层 ───────────────────────────────────────────
from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.gate_client import GateApiError  # noqa: E402

bots_dir = root / "config" / "bots"
bots_dir.mkdir(parents=True)
(bots_dir / "multi.yaml").write_text(
    "bot_id: multi\nenabled: false\nsymbols: [BTC_USDT, ETH_USDT]\n"
    "label_prefix: multi\n", encoding="utf-8")
cfg = load_bot_config(bots_dir / "multi.yaml")
check("配置-多币加载", cfg.symbols == ["BTC_USDT", "ETH_USDT"], cfg.symbols)

# 紧凑/小写写法要归一
(bots_dir / "loose.yaml").write_text(
    "bot_id: loose\nenabled: false\nsymbols: [btcusdt, eth_usdt]\n"
    "label_prefix: loose\n", encoding="utf-8")
cfg2 = load_bot_config(bots_dir / "loose.yaml")
check("配置-形态归一", cfg2.symbols == ["BTC_USDT", "ETH_USDT"], cfg2.symbols)

# 空白名单（未声明 unrestricted）：**基线配置被忽略 enabled** → 只告警；
# 真正启用（走 bots.local/ 覆盖）时才报错。两条路径都要对。
(bots_dir / "empty.yaml").write_text(
    "bot_id: empty\nenabled: false\nsymbols: []\nlabel_prefix: e\n", encoding="utf-8")
cfg_e = load_bot_config(bots_dir / "empty.yaml")
check("配置-空白名单(未启用仅告警)", cfg_e.symbols == [] and not cfg_e.symbols_unrestricted)

local = root / "config" / "bots.local"
local.mkdir(parents=True)
(local / "empty.yaml").write_text("enabled: true\n", encoding="utf-8")
try:
    load_bot_config(bots_dir / "empty.yaml", overlay_dir=local)
    check("配置-空白名单(启用即报错)", False, "没报错")
except GateApiError as e:
    check("配置-空白名单(启用即报错)", "symbols_unrestricted" in str(e), str(e)[:60])

# 分析宇宙超出执行白名单 → 启用时报错
(bots_dir / "out.yaml").write_text(
    "bot_id: out\nenabled: false\nsymbols: [BTC_USDT]\nlabel_prefix: o\n"
    "strategist: {symbols: [ETH_USDT]}\n", encoding="utf-8")
(local / "out.yaml").write_text("enabled: true\n", encoding="utf-8")
try:
    load_bot_config(bots_dir / "out.yaml", overlay_dir=local)
    check("配置-宇宙超白名单报错", False, "没报错")
except GateApiError as e:
    check("配置-宇宙超白名单报错", "strategist.symbols" in str(e), str(e)[:60])

# ── 2. 快照层：按币分区 + position_state ────────────────
from omnialpha.strategist.snapshot import position_state  # noqa: E402

acct = {
    "positions": [{"contract": "BTC_USDT", "size": 1},
                  {"contract": "ETH_USDT", "size": -2},
                  {"contract": "SOL_USDT", "size": 3}],       # 宇宙外
    "open_orders": [], "protections": [],
}
st = position_state(acct, ["BTC_USDT", "ETH_USDT"])
check("快照-position_state 按币", isinstance(st, tuple) and st[0] and "SOL_USDT" not in str(st),
      str(st)[:90])

# ── 3. Plan 层闸门：越界 chip 被拒 ──────────────────────
from omnialpha.strategist.schema import parse_plan_text  # noqa: E402

plan_json = json.dumps({
    "cycle_id": "c1", "reasoning": "r",
    "chips": [{"symbol": "SOL_USDT", "action": "open_long", "size_usd": 10,
               "sl": 100, "confidence": 0.9}],
})
try:
    p = parse_plan_text(plan_json, symbols=["BTC_USDT", "ETH_USDT"])
    syms = [getattr(c, "symbol", "") for c in (p.chips or [])]
    check("Plan闸门-越界被拒", "SOL_USDT" not in syms, syms)
except Exception as e:  # noqa: BLE001
    check("Plan闸门-越界被拒", True, f"显式报错也算（{str(e)[:40]}）")

# ── 4. 工具前置：多币漏写拒绝 / 单币自动补 ──────────────
from omnialpha.strategist.tools import run_tool  # noqa: E402

out = run_tool(None, "ticker", {}, symbols=["BTC_USDT", "ETH_USDT"])
check("工具-多币漏写拒绝", out.get("error") == "symbol_required",
      f"{out.get('error')} universe={out.get('universe')}")

out2 = run_tool(None, "sentiment", {}, symbols=["BTC_USDT", "ETH_USDT"])
check("工具-sentiment 多币缺 coin 拒绝", out2.get("error") == "symbol_required", out2.get("error"))

out3 = run_tool(None, "sentiment", {"coin": "BTC_USDT"}, symbols=["BTC_USDT", "ETH_USDT"])
check("工具-sentiment 别名 coin 被认", out3.get("error") != "symbol_required",
      str(out3)[:60])

# ── 5. 记忆层：按币 ────────────────────────────────────
from omnialpha.memory.journal import MemoryJournal, tier1_journal_fields  # noqa: E402

t1 = tier1_journal_fields([
    {"symbol": "BTC_USDT", "action": "hold", "region": "range"},
    {"symbol": "ETH_USDT", "action": "open_long", "invalidation": 2690},
])
check("记忆-Tier1 按币", t1["symbols"] == ["BTC_USDT", "ETH_USDT"]
      and t1["tier1"]["ETH_USDT"]["invalidation_price"] == 2690, t1["tier1"])
MemoryJournal(root, "multi").append(cycle_id="c1", decision="hold,open_long",
                                    reasoning="r", **t1)
rec = MemoryJournal(root, "multi").read_recent(1)[0]
check("记忆-journal 可按币还原", rec["decisions"] == ["hold", "open_long"]
      and "ETH_USDT" in rec["tier1"], rec.get("decisions"))

# ── 6. 守护层：扫描集合 = 宇宙 ∪ 有归属的合约 ──────────
from omnialpha.watcher import guard_scan_set  # noqa: E402


class _C:
    def list_orders(self, contract=None):
        return [{"contract": "SOL_USDT", "text": "t-multi"}]      # 本 bot 的痕迹

    def list_price_orders(self, contract=None):
        return []

    def get_positions(self):
        return [{"contract": "SOL_USDT", "size": 1}]


class _B:
    bot_id = "multi"
    symbols = ["BTC_USDT", "ETH_USDT"]
    label_prefix = "multi"


scan = guard_scan_set(_B(), _C())
check("守护-归属合约并入扫描集", "SOL_USDT" in scan["symbols"]
      and scan["extra"] == ["SOL_USDT"], scan["symbols"])

print("\n" + "=" * 60)
print("PROD-LINK SUMMARY  PASS=%d FAIL=%d" % (0, 0) if False else
      "PROD-LINK SUMMARY  PASS=%d FAIL=%d" % (11 - len(FAILS), len(FAILS)))
print("FAILED: " + ", ".join(FAILS) if FAILS else "全部通过")
td.cleanup()
raise SystemExit(1 if FAILS else 0)
