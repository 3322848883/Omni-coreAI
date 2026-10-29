#!/usr/bin/env python3
"""本地测试「现在版本」：直接读生产配置 martin-grid.yaml 的 ladder_template，
跑 9 个场景（波动档 / 幂等 / AI 档距 / 固定锚点 / 结构吸附 / SL 护栏 / 数据缺失）。
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
from omnialpha.schema import Intent  # noqa: E402

TPL_PROD = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
MARK = 2332.71
POS = 3.0
RESULTS = []


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK, pos=POS, sl_size=-3):
        self.mark, self.pos = mark, pos
        self.book, self.prot = [], []
        self.total_placed = 0
        self.cancelled = []
        self.left_sl = sl_size
        if sl_size is not None:
            self.prot.append({"id": "old", "status": "open",
                              "trigger": {"price": "2160", "rule": 2},
                              "initial": {"size": sl_size}})

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if not self.pos:
            return []
        return [{"contract": "ETH_USDT", "side": "long", "size": str(abs(self.pos)),
                 "entry_price": str(self.mark)}]

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-mtg"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        rec = dict(body, id=f"o{self.total_placed}")
        self.total_placed += 1
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        self.prot = [p for p in self.prot if p["id"] != pid]
        return {}


class Report:
    def __init__(self):
        self.results = []


def db(atr, bw=2.6, null=False):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    half = MARK * bw / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 None if null else atr, None if null else MARK,
                 None if null else MARK + half, None if null else MARK - half))
    con.commit()
    con.close()
    return f.name


def run(tag, atr=7.5, bw=2.6, null=False, sl=2160.0, meta=None, client=None,
        state=None):
    c = client or Client()
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL_PROD))
    d = dict(TPL_PROD.get("dynamic") or {})
    d["db_path"] = db(atr, bw, null)
    ex.ladder_template = dict(TPL_PROD, dynamic=d)
    # **必须**把锚点状态指到临时目录：生产配置里 anchor_mode=fixed +
    # anchor_state_file=data/ladder_anchor.json，测试若照用会写脏真实锚点
    # （实测本地跑完生成了 data/ladder_anchor.json，线上会直接读到错误锚点）。
    ex.ladder_template.update({
        "anchor_mode": "fixed", "reanchor_threshold_pct": 2.0,
        "anchor_state_file": str(state or (Path(tempfile.mkdtemp()) / "anchor.json")),
    })
    ex._round_meta = dict(meta or {})
    rep = Report()
    leg = Intent(action="open_long", symbol="ETH_USDT", price=MARK * 0.995,
                 size_usd=MARK * 0.01, sl=sl)
    ex._complete_ladder([leg], rep)
    dets = [s.detail for s in rep.results
            if getattr(s, "action", "") == "ladder_template" and s.detail]
    det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
    Path(ex.ladder_template["dynamic"]["db_path"]).unlink(missing_ok=True)
    prices = sorted((float(b["price"]), int(float(b["size"]))) for b in c.book)
    return c, det, prices


def check(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + extra) if extra else ''}")


print("=" * 70)
print("生产配置检查")
print("=" * 70)
for k in ("grid_levels", "grid_step_pct", "martin_slots", "snap_tol_pct",
          "anchor_mode", "reanchor_threshold_pct"):
    print(f"  {k} = {TPL_PROD.get(k)}")
dyn = TPL_PROD.get("dynamic") or {}
print(f"  dynamic = { {k: dyn.get(k) for k in ('atr_mult','bb_div','min_step_pct','max_step_pct','max_span_pct','max_martin_pct')} }")

print("\n" + "=" * 70)
print("① 当前波动（ATR 7.5 / 带宽 2.6%）")
print("=" * 70)
c, det, px = run("now")
print(f"  step={det.get('step_pct')}%  层数={len(px)}  最深={(MARK - px[0][0]) / MARK * 100:.2f}%")
print(f"  张数={[s for _, s in px]}")
print(f"  档位={[round(p, 1) for p, _ in px]}")
check("10 层", len(px) == 10, f"实际 {len(px)}")
sizes_desc = [s for _, s in reversed(px)]     # 从锚点往下
slots_cfg = {int(k): int(v) for k, v in (TPL_PROD.get("martin_slots") or {}).items()}
want_sizes = [slots_cfg.get(i + 1, int(TPL_PROD.get("grid_lots") or 1))
              for i in range(len(sizes_desc))]
check("马丁占位与配置一致", sizes_desc == want_sizes,
      f"实际{sizes_desc} 期望{want_sizes}（slots={slots_cfg}）")
gaps = [round(px[i][0] - px[i + 1][0], 2) for i in range(len(px) - 1)]
check("间距基本等距", max(gaps) - min(gaps) < 1.0, f"{gaps}")
prot = c.prot[-1]["initial"]["size"] if c.prot else None
check("SL 覆盖 = 持仓+入场", prot == -(POS + sum(s for _, s in px)), f"SL={prot}")

print("\n" + "=" * 70)
print("② 低波动 / ③ 高波动 / ④ 指标缺失")
print("=" * 70)
for tag, atr, bw, null in (("低波动 ATR2", 2.0, 0.8, False),
                           ("高波动 ATR40", 40.0, 12.0, False),
                           ("指标缺失", 0.0, 0.0, True)):
    c2, det2, px2 = run(tag, atr=atr, bw=bw, null=null)
    print(f"  {tag}: step={det2.get('step_pct')}% 层数={len(px2)} "
          f"deepest={(MARK - px2[0][0]) / MARK * 100:.2f}%")
    check(f"{tag} 10 层", len(px2) == 10, f"实际 {len(px2)}")
    check(f"{tag} step 合法", (det2.get("step_pct") or 0) <= 0.86 + 1e-6)

print("\n" + "=" * 70)
print("⑤ 幂等（同盘面连跑两次）")
print("=" * 70)
c3 = Client()
st3 = Path(tempfile.mkdtemp()) / "anchor.json"      # 同一 bot：锚点必须跨轮保持
run("a", client=c3, state=st3)
n1 = c3.total_placed
_, det3, _ = run("b", client=c3, state=st3)
check("第二次不重复挂", c3.total_placed == n1, f"{n1} → {c3.total_placed}")
check("标记 already_full", det3.get("skipped") == "already_full", str(det3.get("skipped")))

print("\n" + "=" * 70)
print("⑥ AI 档距建议（clamp）")
print("=" * 70)
_, det4, px4 = run("ai", meta={"ladder_step_pct": 0.55})
check("采用 AI 0.55（区间内）", abs((det4.get("step_pct") or 0) - 0.55) < 1e-6,
      str(det4.get("step_pct")))
_, det4b, _ = run("ai0", meta={"ladder_step_pct": 0.7})
check("0.7 被跨度上限 0.6 截断",
      abs((det4b.get("step_pct") or 0) - 0.6) < 1e-6
      and (det4b.get("dynamic") or {}).get("ai_step_clamped") is True,
      str(det4b.get("step_pct")))
_, det5, _ = run("ai2", meta={"ladder_step_pct": 5.0})
check("越界被 clamp", (det5.get("step_pct") or 0) <= 0.86 + 1e-6
      and (det5.get("dynamic") or {}).get("ai_step_clamped") is True,
      str((det5.get("dynamic") or {}).get("ai_step_clamped")))

print("\n" + "=" * 70)
print("⑦ 固定锚点 4 轮")
print("=" * 70)
state = Path(tempfile.mkdtemp()) / "anchor.json"
c7 = Client()
tot = []
for i, mk in enumerate((MARK, MARK + 1, MARK * 1.018, MARK * 1.022)):
    c7.mark = mk
    _, det7, _ = run(f"r{i}", client=c7, state=state)
    tot.append((round(mk, 2), det7.get("anchor"), c7.total_placed))
print(f"  {tot}")
check("第 2/3 轮不重铺", tot[1][2] == tot[0][2] and tot[2][2] == tot[0][2])
check("第 4 轮重锚后仍 10 层", len(c7.book) == 10, f"{len(c7.book)}")

print("\n" + "=" * 70)
print("⑧ 结构吸附 + ⑨ SL 护栏")
print("=" * 70)
_, det8, px8 = run("snap", meta={"structure_levels": [2330, 2312, 2296, 2280, 2262]})
snap = (det8.get("dynamic") or {}).get("structure_snap") or {}
print(f"  吸附={snap}  层数={len(px8)}")
check("吸附生效", snap.get("snapped", 0) >= 2, str(snap))
check("吸附后仍 10 层", len(px8) == 10, f"{len(px8)}")
# 断言改口径：SL 现在走 sl_distance 点数模式（300~500 点），不再用 ATR 的 bounds
dyn8 = det8.get("dynamic") or {}
sp8 = dyn8.get("sl_points") or {}
print(f"  SL 点数={sp8}")
check("SL 走点数模式", sp8.get("mode") == "points", str(sp8))
check("覆盖价距 300~500 点",
      bool(sp8) and 300 <= (sp8.get("points") or 0) <= 500, str(sp8.get("points")))

print("\n" + "=" * 70)
print(f"总计：{sum(RESULTS)}/{len(RESULTS)} 项通过")
sys.exit(0 if all(RESULTS) else 1)
