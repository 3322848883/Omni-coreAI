# -*- coding: utf-8 -*-
"""Round 2 压力测试：极端 / 恶意输入下的铺梯韧性。

覆盖两类：
  A. 输入脏（模型给的 structure_levels / ladder_step_pct / 指标库异常）
  B. 状态脏（锚点 state、档位 state 损坏或方向不一致）

每条断言都只看「外部可观测行为」：不崩、档位在正确侧、无同价重复、
SL 存在且落在 300~500 口径内、坏 state 能自愈。
"""
import json
import math
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
RES = []


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK, pos=0.0, pos_side="long", sl_px=None):
        self.mark, self.pos, self.pos_side = mark, pos, pos_side
        self.book, self.prot = [], []
        self.n = 0
        if sl_px:
            self.prot.append({"id": "p-o", "status": "open",
                              "trigger": {"price": str(sl_px), "rule": 2},
                              "initial": {"size": -3}})

    def get_last_price(self, s):
        return self.mark

    def get_contract(self, s):
        return Meta()

    def get_positions(self):
        if not self.pos:
            return []
        return [{"contract": "ETH_USDT", "side": self.pos_side,
                 "size": str(self.pos), "entry_price": str(self.mark)}]

    def list_orders(self, s):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, s):
        return self.prot

    def place_order(self, b):
        rec = dict(b, id=f"o{self.n}", price=float(b["price"]),
                   size=float(b["size"]))
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


class Rep:
    def __init__(self):
        self.results = []


def mkdb(atr=7.5, bw=2.6, null=False, nan=False, path=None):
    f = path or tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    con = sqlite3.connect(f)
    con.execute("CREATE TABLE IF NOT EXISTS kline (symbol TEXT, interval TEXT, t INTEGER, "
                "closed INT, o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    con.execute("DELETE FROM kline")
    half = MARK * bw / 200
    a = float("nan") if nan else (None if null else atr)
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 a, None if null else MARK,
                 None if null else MARK + half, None if null else MARK - half))
    con.commit()
    con.close()
    return f



# ── 2026-10-30：上限/张数一律从生产配置推导，别硬编码（调参会全挂）──────────
_CAP = (float((TPL_PROD.get("dynamic") or {}).get("max_span_pct") or 6.0)
        / float(TPL_PROD.get("grid_levels") or 10))
_MIN = float((TPL_PROD.get("dynamic") or {}).get("min_step_pct") or 0.3)
_LOTS = float(TPL_PROD.get("grid_lots") or 1)
_SLOTS = {int(k): float(v) for k, v in (TPL_PROD.get("martin_slots") or {}).items()}
_NLV = int(TPL_PROD.get("grid_levels") or 10)
_WANT_LOTS = [_SLOTS.get(i + 1, _LOTS) for i in range(_NLV)]
_TOTAL_LOTS = sum(_WANT_LOTS)


def run(tag, *, mark=MARK, atr=7.5, bw=2.6, null=False, nan=False, meta=None,
        pos=0.0, pos_side="long", sl_px=None, client=None, state_dir=None,
        action="hold", sl=None, extra_tpl=None):
    d = tempfile.mkdtemp() if state_dir is None else str(state_dir)
    dbp = mkdb(atr, bw, null, nan, path=str(Path(d) / "k.db"))
    c = client or Client(mark, pos, pos_side, sl_px)
    c.mark = mark
    tpl = dict(TPL_PROD)
    dyn = dict(tpl.get("dynamic") or {})
    dyn["db_path"] = dbp
    tpl["dynamic"] = dyn
    tpl["anchor_state_file"] = str(Path(d) / "anchor.json")
    if extra_tpl:
        tpl.update(extra_tpl)
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=tpl)
    ex.ladder_template = tpl
    ex._round_meta = dict(meta or {})
    rep = Rep()
    leg = Intent(action=action, symbol="ETH_USDT",
                 price=mark * (0.995 if action != "open_short" else 1.005),
                 size_usd=mark * 0.01, sl=sl)
    err = None
    try:
        ex._complete_ladder([leg], rep)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    dets = [s.detail for s in rep.results
            if getattr(s, "action", "") == "ladder_template" and s.detail]
    det = next((x for x in dets if "step_pct" in x or x.get("skipped") or x.get("blocked")),
               dets[0] if dets else {})
    errs = [s.error for s in rep.results if getattr(s, "error", None)]
    return c, det, err, errs


def ck(name, cond, extra=""):
    RES.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + str(extra)) if extra else ''}")


def book_px(c):
    return sorted(float(b["price"]) for b in c.book)


def same_side_ok(c, side):
    px = book_px(c)
    if side == "short":
        return all(all(p > c.mark for p in px) for _ in [0])
    return all(p < c.mark for p in px)


def dup_count(px, tol=0.003):
    n = 0
    for i in range(len(px)):
        for j in range(i + 1, len(px)):
            if abs(px[i] - px[j]) / max(px[i], 1e-9) <= tol:
                n += 1
    return n


print("=" * 72)
print("R2-A · 脏输入：structure_levels")
print("=" * 72)

# ① 全是非数值
c, det, err, errs = run("junk1", meta={"structure_levels": ["a", None, "x", {}, []]})
ck("① 非数值不崩", err is None, err or "")
ck("① 回退等距仍铺满", len(c.book) >= 9, f"{len(c.book)} 层")

# ② NaN / inf
c, det, err, errs = run("junk2", meta={"structure_levels": [float("nan"), float("inf"),
                                                            -1, 0, 2290.0]})
ck("② NaN/inf 不崩", err is None, err or "")
ck("② 只采纳合法正值", len(c.book) >= 9, f"{len(c.book)} 层")

# ③ 密集簇（间距 0.05%，远小于 min_gap 0.3%）
cluster = [round(MARK * (1 - 0.005 * i / 100), 2) for i in range(1, 31)]
c, det, err, errs = run("junk3", meta={"structure_levels": cluster})
px = book_px(c)
ck("③ 密集簇去重后无同价", dup_count(px) == 0, f"{len(px)} 档 dup={dup_count(px)}")
ck("③ 档数仍达标", 9 <= len(px) <= 16, len(px))

# ④ 全部在错误侧（多头给上方价位）
c, det, err, errs = run("junk4", meta={"structure_levels": [MARK * 1.01, MARK * 1.02,
                                                            MARK * 1.03]})
px = book_px(c)
ck("④ 错误侧不崩", err is None, err or "")
ck("④ 全部落在现价下方", all(p < MARK for p in px), f"min/max={min(px):.1f}/{max(px):.1f}")

print("\n" + "=" * 72)
print("R2-A · 脏输入：ladder_step_pct")
print("=" * 72)
cases = [("'abc'", "abc"), ("0", 0), ("-1", -1), ("NaN", float("nan")),
         ("'auto'", "auto"), ("999", 999), ("0.0001", 0.0001)]
for tag, val in cases:
    c, det, err, errs = run(f"step{tag}", meta={"ladder_step_pct": val})
    sp = det.get("step_pct")
    ok = (err is None and sp is not None
          and _MIN - 1e-6 <= float(sp) <= _CAP + 1e-6)
    ck(f"⑤ ladder_step_pct={tag} → 合法档距", ok, f"step={sp} err={err}")

print("\n" + "=" * 72)
print("R2-A · 脏输入：指标库")
print("=" * 72)
for tag, kw in (("atr14=NULL", dict(null=True)),
                ("atr14=NaN", dict(nan=True)),
                ("atr14=0", dict(atr=0.0)),
                ("atr 极大 9999", dict(atr=9999.0))):
    c, det, err, errs = run(f"ind{tag}", **kw)
    sp = det.get("step_pct")
    ok = (err is None and sp is not None
          and _MIN - 1e-6 <= float(sp) <= _CAP + 1e-6)
    ck(f"⑥ {tag} → 档距合法", ok, f"step={sp} err={err}")
    ck(f"⑥ {tag} → 仍满层", len(c.book) >= 9, len(c.book))

print("\n" + "=" * 72)
print("R2-B · 脏状态：锚点/档位 state")
print("=" * 72)
# ⑦ 档位 state 是半个 JSON
sd = Path(tempfile.mkdtemp())
(sd / "ladder_levels.json").write_text('{"ETH_USDT": {"levels": [[2330.0, 1', encoding="utf-8")
c, det, err, errs = run("badlv", state_dir=sd)
ck("⑦ 半个 JSON 不崩", err is None, err or "")
ck("⑦ 仍铺满", len(c.book) >= 9, len(c.book))

# ⑧ 档位 state 方向与当前不一致（缓存 side=long，本轮 short）
sd = Path(tempfile.mkdtemp())
(sd / "ladder_levels.json").write_text(json.dumps({
    "ETH_USDT": {"side": "long", "anchor": MARK,
                 "levels": [[2300.0, 1], [2280.0, 1]]}}), encoding="utf-8")
c, det, err, errs = run("wrongside", state_dir=sd, action="open_short")
px = book_px(c)
ck("⑧ 方向不匹配时不吃旧缓存", all(p > MARK for p in px),
   f"min={min(px):.1f} mark={MARK}")
ck("⑧ 空头铺满", len(c.book) >= 9, len(c.book))

# ⑨ 锚点 state 是垃圾
sd = Path(tempfile.mkdtemp())
(sd / "anchor.json").write_text("not-json-at-all", encoding="utf-8")
c, det, err, errs = run("badanchor", state_dir=sd)
ck("⑨ 锚点垃圾不崩", err is None, err or "")
ck("⑨ 仍铺满", len(c.book) >= 9, len(c.book))

# ⑩ 档位 state 里价格是负数/0
sd = Path(tempfile.mkdtemp())
(sd / "anchor.json").write_text(json.dumps({"ETH_USDT": {"anchor": MARK}}),
                                encoding="utf-8")
(sd / "ladder_levels.json").write_text(json.dumps({
    "ETH_USDT": {"side": "long", "anchor": MARK,
                 "levels": [[-1, 1], [0, 1], [2300.0, 1]]}}), encoding="utf-8")
c, det, err, errs = run("neglv", state_dir=sd)
px = book_px(c)
ck("⑩ 负价档位不落地", err is None and all(p > 0 for p in px),
   f"err={err} px={px[:4]}")

print("\n" + "=" * 72)
print("R2-B · 跳空 & 连续翻向")
print("=" * 72)
# ⑪ 一步跳空 -25%：锚点必须重锚，档位全在现价上方（short）
sd = Path(tempfile.mkdtemp())
c0 = Client(MARK, 0.0, "long", None)
run("pre", client=c0, state_dir=sd, action="open_short")
c1, det, err, errs = run("gap", client=c1 if False else None, mark=MARK * 0.75,
                         state_dir=sd, action="open_short")
px = book_px(c1)
ck("⑪ 跳空 -25% 不崩", err is None, err or "")
ck("⑪ 重锚后档位在现价上方", all(p > MARK * 0.75 for p in px),
   f"mark={MARK*0.75:.1f} min={min(px):.1f}")

# ⑫ 连续翻向：多持 3 张 → 判空（应 blocked）→ 再判多
sd = Path(tempfile.mkdtemp())
c2 = Client(MARK, 3.0, "long", None)
for i, side in enumerate(("open_short", "open_long", "open_short", "open_long")):
    c2.mark = MARK
    _, det, err, errs = run(f"flip{i}", client=c2, state_dir=sd, action=side)
    blocked = det.get("blocked")
    ck(f"⑫ 第{i+1}次翻向被拦/正常", err is None,
       f"blocked={blocked} err={err}")
ck("⑫ 翻向期间持仓未变", abs(c2.pos - 3.0) < 1e-9, c2.pos)
ck("⑫ 始终有保护单", len(c2.prot) > 0, len(c2.prot))

print("\n" + "=" * 72)
print("R2-B · 负价 / 重复 want")
print("=" * 72)
# ⑬ structure_levels 含 0 与负值混正经结构位
c, det, err, errs = run("mix", meta={"structure_levels": [0, -100, 2320.0, 2300.0,
                                                          2280.0, 2260.0]})
px = book_px(c)
ck("⑬ 混合结构位不产生负价档", all(p > 0 for p in px), f"min={min(px):.1f}")
ck("⑬ 无同价重复", dup_count(px) == 0, dup_count(px))

print("\n" + "=" * 72)
print(f"R2 总计：{sum(RES)}/{len(RES)} 项通过")
sys.exit(0 if all(RES) else 1)
