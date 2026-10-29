# -*- coding: utf-8 -*-
"""Round 3 端到端：配置生效 → 信号链路 → 执行器铺梯。

与 Round 1/2 的区别：这里不直接调 `_complete_ladder`，而是从
`load_bot_config` 取真实配置、构造 0 单 SignalFile（模拟 always_on 的 hold 轮）、
走 `execute_signal` 全链路，验证「配置改了到底会不会生效」。
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import SignalFile  # noqa: E402

cfg = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
TPL = dict(cfg.ladder_template or {})
MARK = 2332.71
RES = []


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK):
        self.mark = mark
        self.book, self.prot = [], []
        self.n = 0

    def get_last_price(self, s):
        return self.mark

    def get_contract(self, s):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, s):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, s):
        return self.prot

    def place_order(self, b):
        rec = dict(b, id=f"o{self.n}", price=float(b["price"]), size=float(b["size"]))
        self.n += 1
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, b):
        rec = {"id": f"p{self.n}", "status": "open",
               "initial": b["initial"], "trigger": b["trigger"]}
        self.n += 1
        self.prot.append(rec)
        return {"id": rec["id"]}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, oid):
        self.prot = [p for p in self.prot if p["id"] != oid]
        return {}


def ck(name, cond, extra=""):
    RES.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + str(extra)) if extra else ''}")


print("=" * 72)
print("R3-1 · 生产配置生效检查（overlay 污染）")
print("=" * 72)
overlay = ROOT / "config/bots.local/martin-grid.yaml"
print(f"  overlay 存在: {overlay.exists()}")
must = {
    "enabled": True, "grid_levels": 10, "always_on": True,
    "anchor_mode": "fixed", "symbol": "ETH_USDT",
}
for k, v in must.items():
    ck(f"{k} == {v}", TPL.get(k) == v, TPL.get(k))
ck("martin_slots 为 3 档占位", len(TPL.get("martin_slots") or {}) == 3,
   TPL.get("martin_slots"))
ck("sl_distance 已开", (TPL.get("sl_distance") or {}).get("enabled") is True,
   TPL.get("sl_distance"))
ck("levels_dynamic 已开", (TPL.get("levels_dynamic") or {}).get("enabled") is True,
   (TPL.get("levels_dynamic") or {}).get("enabled"))
dyn = TPL.get("dynamic") or {}
ck("dynamic.enabled", dyn.get("enabled") is True, dyn.get("enabled"))
ck("dynamic.max_span_pct 有值", float(dyn.get("max_span_pct") or 0) > 0,
   dyn.get("max_span_pct"))
ck("reanchor_threshold_pct=2.0", float(TPL.get("reanchor_threshold_pct") or 0) == 2.0,
   TPL.get("reanchor_threshold_pct"))
print(f"  direction_mode = {TPL.get('direction_mode')}  "
      f"(auto 会按 4H 趋势自动翻向；signal 跟随模型)")

print("\n" + "=" * 72)
print("R3-2 · 0 单信号（always_on hold 轮）全链路")
print("=" * 72)
d = Path(tempfile.mkdtemp())
dbp = d / "k.db"
con = sqlite3.connect(dbp)
con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
            "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
            "boll20_upper REAL, boll20_lower REAL)")
half = MARK * 2.6 / 200
con.execute("INSERT INTO kline VALUES ('ETH_USDT','15m',1793170000,1,?,?,?,?,?,?,?,?)",
            (MARK, MARK + 5, MARK - 5, MARK, 7.5, MARK,
             MARK + half, MARK - half))
con.commit()
con.close()

tpl = dict(TPL)
tpl["dynamic"] = dict(dyn, db_path=str(dbp))
tpl["anchor_state_file"] = str(d / "anchor.json")
tpl.setdefault("sl_pct", 5.0)

c = Client()
ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
ex.ladder_template = tpl
sig = SignalFile(intents=[], meta={}, raw={}, replace="none")
rep = ex.execute_signal(sig)
errs = [r.error for r in rep.results if getattr(r, "error", None)]
print(f"  results={len(rep.results)} errors={errs[:3]}")
ck("0 单轮不报错", not errs, errs[:2])
px = sorted(float(b["price"]) for b in c.book)
ck("0 单轮仍铺出阶梯（always_on 生效）", len(px) >= 9, f"{len(px)} 层")
ck("档位全在现价下方（多头/signal 默认）", all(p < MARK for p in px) if px else False,
   f"min={min(px):.1f}" if px else "no orders")
ck("有保护单", len(c.prot) > 0, len(c.prot))
if c.prot:
    trg = c.prot[-1]["trigger"]
    sl = float(trg["price"])
    deepest = min(px) if px else MARK
    gap = abs(sl - deepest)
    ck("SL 覆盖价距 300~500 点", 290 <= gap <= 510, f"gap={gap:.1f} sl={sl:.1f}")
    ck("SL rule 方向正确", int(trg.get("rule") or 0) == 2, trg.get("rule"))

print("\n" + "=" * 72)
print("R3-3 · 同一盘面连跑 5 轮（幂等 + 档位不漂移）")
print("=" * 72)
snaps = []
for i in range(5):
    rep = ex.execute_signal(SignalFile(intents=[], meta={}, raw={}, replace="none"))
    snaps.append((len(c.book), tuple(sorted(round(float(b["price"]), 1)
                                            for b in c.book)),
                  len(c.prot)))
for i, s in enumerate(snaps):
    print(f"  轮{i+1}: 档位={s[0]} SL={s[2]}")
ck("5 轮档位数稳定", len({s[0] for s in snaps}) == 1, {s[0] for s in snaps})
ck("5 轮档位价不漂移", len({s[1] for s in snaps}) == 1,
   "drift" if len({s[1] for s in snaps}) > 1 else "stable")
ck("5 轮 SL 条数不增长", len({s[2] for s in snaps}) == 1, {s[2] for s in snaps})

print("\n" + "=" * 72)
print("R3-4 · 状态文件落盘位置正确（不污染生产 data/）")
print("=" * 72)
levels_state = d / "ladder_levels.json"
ck("档位 state 写在 anchor 同目录", levels_state.exists(), levels_state)
if levels_state.exists():
    data = json.loads(levels_state.read_text(encoding="utf-8"))
    rec = data.get("ETH_USDT") or {}
    ck("state 含 side/anchor/levels", all(k in rec for k in ("side", "anchor", "levels")),
       list(rec.keys()))
    ck("state 档位数为 9~15", 9 <= len(rec.get("levels") or []) <= 15,
       len(rec.get("levels") or []))
prod_state = ROOT / "data/ladder_levels.json"
ck("生产 data/ 未被测试污染", not prod_state.exists(), prod_state)

print("\n" + "=" * 72)
print(f"R3 总计：{sum(RES)}/{len(RES)} 项通过")
sys.exit(0 if all(RES) else 1)
