#!/usr/bin/env python3
"""B2/B3 本地矩阵：方向（迟滞/双向）+ 边界异常，全部从**生产配置**出发。

与 _local_suite.py 互补：那个测阶梯结构（层数/档距/锚点/马丁/SL），
这个测方向链路（4H 迟滞 → 铺单符号与位置 → SL 规则）和异常韧性。

跑：python scripts/_local_matrix_v2.py
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

TPL = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
MARK = 2332.71
EMA200 = 2454.0
RESULTS = []


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK, pos=0.0, sl_size=None, side="long"):
        self.mark, self.pos, self.side = mark, pos, side
        self.book, self.prot = [], []
        self.total_placed = 0
        self.cancelled = []
        if sl_size is not None:
            rule = 2 if side == "long" else 1
            self.prot.append({"id": "old", "status": "open",
                              "trigger": {"price": "0", "rule": rule},
                              "initial": {"size": sl_size}})

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        if not self.pos:
            return []
        signed = abs(self.pos) if self.side == "long" else -abs(self.pos)
        return [{"contract": "ETH_USDT", "side": self.side,
                 "size": str(signed), "entry_price": str(self.mark)}]

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


def db2(close4h=None, ema200=EMA200, atr=7.5, bw=2.6, null15=False, with4h=True):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute(
        "CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
        "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, boll20_upper REAL, "
        "boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
    half = MARK * bw / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793272500, 1, MARK, MARK + 5, MARK - 5, MARK,
                 None if null15 else atr,
                 None if null15 else MARK,
                 None if null15 else MARK + half,
                 None if null15 else MARK - half,
                 None, MARK * 0.999, MARK * 0.998))
    if with4h and close4h is not None:
        con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("ETH_USDT", "4h", 1793246400, 1, close4h, close4h, close4h,
                     close4h, None, None, None, None, ema200, None, None))
    con.commit()
    con.close()
    return f.name



# ── 2026-10-30：上限/张数一律从生产配置推导，别硬编码（调参会全挂）──────────
_CAP = (float((TPL.get("dynamic") or {}).get("max_span_pct") or 6.0)
        / float(TPL.get("grid_levels") or 10))
_MIN = float((TPL.get("dynamic") or {}).get("min_step_pct") or 0.3)
_LOTS = float(TPL.get("grid_lots") or 1)
_SLOTS = {int(k): float(v) for k, v in (TPL.get("martin_slots") or {}).items()}
_NLV = int(TPL.get("grid_levels") or 10)
_WANT_LOTS = [_SLOTS.get(i + 1, _LOTS) for i in range(_NLV)]
_TOTAL_LOTS = sum(_WANT_LOTS)


def build(client, db_path, state_dir, tpl_extra=None, entry=None, meta=None):
    ex = Executor(client, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
    t = dict(TPL)
    t["dynamic"] = dict(TPL.get("dynamic") or {}, db_path=db_path)
    t["anchor_state_file"] = str(Path(state_dir) / "anchor.json")
    t["direction_hysteresis"] = dict(t.get("direction_hysteresis") or {},
                                     state_file=str(Path(state_dir) / "dir.json"))
    t.update(tpl_extra or {})
    ex.ladder_template = t
    ex._round_meta = dict(meta or {})
    rep = Report()
    ex._complete_ladder(entry if entry is not None else [
        Intent(action="hold", symbol="ETH_USDT")], rep)
    dets = [s.detail for s in rep.results
            if getattr(s, "action", "") == "ladder_template" and s.detail]
    det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
    errs = [getattr(s, "error", None) for s in rep.results if getattr(s, "error", None)]
    return ex, det, errs


def check(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + extra) if extra else ''}")


# ══════════════════════════════════════════════════════════════════════════
print("=" * 72)
print("B2 · 方向矩阵（迟滞 × 四种 4H 收盘 × 双向铺单）")
print("=" * 72)

sd = tempfile.mkdtemp()

# ① 4H 在 EMA200 下方 → short，铺上方、size 负、SL 在上方 rule=1
d = db2(close4h=EMA200 * 0.978)          # −2.2%
c = Client()
_, det, errs = build(c, d, sd, {"direction_mode": "auto"})
sizes = sorted(float(b["size"]) for b in c.book)
prices = sorted(float(b["price"]) for b in c.book)
sides = (det.get("dynamic") or {}).get("side")
check("① 4H −2.2% → side=short", sides == "short", f"side={sides}")
check("① 层数 10", len(c.book) == 10, f"{len(c.book)}")
check("① 全部 size 为负", all(s < 0 for s in sizes), f"{sizes[:3]}")
check("① 全部铺在现价上方", all(p > c.mark for p in prices), f"min={prices[0]:.1f} mark={c.mark}")
sl = [p for p in c.prot if (p.get("trigger") or {}).get("rule") == 1]
check("① SL rule=1 涨破买入", bool(sl), f"{len(sl)} 条 rule=1")
if sl:
    check("① SL 在最高档之上",
          float(sl[-1]["trigger"]["price"]) > max(prices),
          f"sl={sl[-1]['trigger']['price']} top={max(prices):.1f}")
    check("① SL size 为正(空头平仓买)",
          float(sl[-1]["initial"]["size"]) > 0, str(sl[-1]["initial"]["size"]))
check("① 无错误", not errs, str(errs[:1]))
Path(d).unlink(missing_ok=True)

# ② 同 state 再跑，4H 反弹到带内（EMA×1.003）→ 仍 short，且盘面不动
d = db2(close4h=EMA200 * 1.003)
ex2, det2, _ = build(c, d, sd, {"direction_mode": "auto"})
# already_full 分支不带 side，所以直接调判定函数断言（弱断言会放过 bug：old 版写 `in (short, None)`）
_dd, _dn = ex2._auto_direction("ETH_USDT", c.mark,
                               {"interval": "15m", "db_path": d},
                               ex2.ladder_template)
check("② 带内反弹不翻向", _dd == "short", f"side={_dd} {_dn.get('reason')}")
check("② 盘面不变(幂等)", len(c.book) == 10 and det2.get("skipped") == "already_full",
      f"{len(c.book)} skipped={det2.get('skipped')}")
Path(d).unlink(missing_ok=True)

# ③ 4H 涨破 1.005 带 → 翻 long，铺下方、size 正、SL rule=2
d = db2(close4h=EMA200 * 1.01)
c3 = Client()
_, det3, _ = build(c3, d, sd, {"direction_mode": "auto"})
s3 = sorted(float(b["size"]) for b in c3.book)
p3 = sorted(float(b["price"]) for b in c3.book)
check("③ 涨破 → side=long", (det3.get("dynamic") or {}).get("side") == "long",
      f"{(det3.get('dynamic') or {}).get('side')}")
check("③ 全部 size 为正", s3 and all(x > 0 for x in s3), f"{s3[:3]}")
check("③ 全部铺在现价下方", p3 and all(x < c3.mark for x in p3), f"max={p3[-1]:.1f}")
sl3 = [p for p in c3.prot if (p.get("trigger") or {}).get("rule") == 2]
check("③ SL rule=2 跌破卖出", bool(sl3), f"{len(sl3)}")
Path(d).unlink(missing_ok=True)

# ④ hysteresis 关 → 回退 BB 裸判，note 里 rule=bb_mid+ema
d = db2(close4h=EMA200 * 0.978)
c4 = Client()
_, det4, _ = build(c4, d, tempfile.mkdtemp(), {
    "direction_mode": "auto",
    "direction_hysteresis": {"enabled": False, "interval": "4h",
                             "state_file": str(Path(tempfile.mkdtemp()) / "d.json")},
})
check("④ 关迟滞 → BB 裸判", (det4.get("dynamic") or {}).get("auto_direction", {}).get("rule")
      == "bb_mid+ema", str((det4.get("dynamic") or {}).get("auto_direction"))[:80])
Path(d).unlink(missing_ok=True)

# ⑤ 无 4H 行 → fallback，仍铺满（不崩）
d = db2(close4h=None, with4h=False)
c5 = Client()
_, det5, errs5 = build(c5, d, tempfile.mkdtemp(), {"direction_mode": "auto"})
ad5 = (det5.get("dynamic") or {}).get("auto_direction", {})
check("⑤ 无 4H 行 → 回退不留崩溃", "hysteresis_fallback" in ad5, str(ad5)[:80])
check("⑤ 回退后仍铺满 10 层", len(c5.book) == 10, f"{len(c5.book)}")
check("⑤ 无错误", not errs5, str(errs5[:1]))
Path(d).unlink(missing_ok=True)

# ⑥ 生产默认 signal：模型给 open_long 就是多，与 4H 无关
d = db2(close4h=EMA200 * 0.978)     # 4H 明明是空
c6 = Client()
_, det6, _ = build(c6, d, tempfile.mkdtemp(), {"direction_mode": "signal"},
                   entry=[Intent(action="open_long", symbol="ETH_USDT",
                                 price=MARK * 0.995, size_usd=MARK * 0.01,
                                 sl=MARK * 0.95)])
s6 = [float(b["size"]) for b in c6.book]
check("⑥ signal 模式跟随模型(多)", s6 and all(x > 0 for x in s6), f"{s6[:3]}")
check("⑥ 4H 空不覆盖模型", (det6.get("dynamic") or {}).get("side") == "long",
      str((det6.get("dynamic") or {}).get("side")))
Path(d).unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("B3 · 边界与异常韧性")
print("=" * 72)

# ① 方向 state 损坏 → 当首次，不崩
sd1 = tempfile.mkdtemp()
Path(sd1, "dir.json").write_text("{ this is not json", encoding="utf-8")
d = db2(close4h=EMA200 * 0.978)
c = Client()
_, det, errs = build(c, d, sd1, {"direction_mode": "auto"})
check("① 坏 state 不崩，按位置初始化",
      (det.get("dynamic") or {}).get("side") == "short" and not errs,
      f"side={(det.get('dynamic') or {}).get('side')} err={errs[:1]}")
check("① 坏 state 被覆写成合法 JSON",
      json.loads(Path(sd1, "dir.json").read_text(encoding="utf-8"))["ETH_USDT"]["side"] == "short")
Path(d).unlink(missing_ok=True)

# ② state 目录不存在 → 自动创建
sd2 = Path(tempfile.mkdtemp()) / "deep" / "nested"
d = db2(close4h=EMA200 * 0.978)
c = Client()
_, det, errs = build(c, d, sd2, {"direction_mode": "auto"})
check("② 目录不存在自动创建", (sd2 / "dir.json").exists(), str(sd2))
check("② 无错误", not errs, str(errs[:1]))
Path(d).unlink(missing_ok=True)

# ③ db 文件不存在 → 动态档距与方向都回退，仍铺 10 层（不能崩）
sd3 = tempfile.mkdtemp()
c = Client()
_, det, errs = build(c, str(Path(tempfile.mkdtemp()) / "nope.db"), sd3,
                     {"direction_mode": "auto"})
check("③ db 缺失 → 不崩", not errs, str(errs[:1]))
# 回退也要守下限：base_step(grid_step_pct=0.5) < min_step(0.66) 时用 min_step，
# 否则「指标读不到」这种最该保守的场景反而铺出最密的梯（实测 11 点 < 15 点下限）。
_fb = max(float(TPL.get("grid_step_pct")), _MIN)
check("③ db 缺失 → 档距回退且守下限",
      abs((det.get("step_pct") or 0) - _fb) < 1e-6,
      f"step={det.get('step_pct')} 期望{_fb}")
check("③ db 缺失 → 仍铺满 10 层", len(c.book) == 10, f"{len(c.book)}")

# ④ 连续两轮同盘面 → 第二轮 0 新增（幂等，锚点未漂移）
sd4 = tempfile.mkdtemp()
d = db2(close4h=EMA200 * 1.01)
c = Client()
build(c, d, sd4, {"direction_mode": "auto"})
n1 = c.total_placed
_, det4b, _ = build(c, d, sd4, {"direction_mode": "auto"})
check("④ 第二轮 0 新增", c.total_placed == n1, f"{n1}→{c.total_placed}")
check("④ 第二轮 already_full", det4b.get("skipped") == "already_full", str(det4b.get("skipped")))
Path(d).unlink(missing_ok=True)

# ⑤ 马丁占位与上限
sd5 = tempfile.mkdtemp()
d = db2(close4h=EMA200 * 1.01, atr=40, bw=12.0)         # 极高波动
c = Client()
_, det5, _ = build(c, d, sd5, {"direction_mode": "auto"})
desc = [round(float(b["size"]), 2) for b in sorted(c.book, key=lambda x: -float(x["price"]))]
check("⑤ 马丁占第 3/7/10 档", desc == _WANT_LOTS,
      f"{desc} 期望{_WANT_LOTS}")
hi_px = max(float(b["price"]) for b in c.book)
lo_px = min(float(b["price"]) for b in c.book)
span_pct = (hi_px - lo_px) / c.mark * 100
check(f"⑤ 极高波动 step 被 span 封顶 {_CAP:.2f}%",
      abs((det5.get("step_pct") or 0) - _CAP) < 0.02, f"step={det5.get('step_pct')}")
check(f"⑤ 档位跨度≈{_CAP * 9:.1f}%",
      abs(span_pct - _CAP * 9) < 0.3, f"{span_pct:.2f}%")
Path(d).unlink(missing_ok=True)

# ⑥ SL 覆盖：多头 持仓3 + 挂单29 → −32
sd6 = tempfile.mkdtemp()
d = db2(close4h=EMA200 * 1.01)
c = Client(pos=3.0, side="long")
build(c, d, sd6, {"direction_mode": "auto"})
sl6 = [p for p in c.prot if (p.get("trigger") or {}).get("rule") == 2]
import math as _m
_need6 = _m.ceil(3.0 + _TOTAL_LOTS - 1e-9)
check(f"⑥ 多头 SL 覆盖 3+{_TOTAL_LOTS:g}={_need6}",
      bool(sl6) and abs(float(sl6[-1]["initial"]["size"])) == float(_need6),
      str(sl6[-1]["initial"]["size"]) if sl6 else "无")
Path(d).unlink(missing_ok=True)

# ⑦ 空头 SL 覆盖（空单 left 为负数的回归守门）
sd7 = tempfile.mkdtemp()
d = db2(close4h=EMA200 * 0.978)
c = Client(pos=3.0, side="short")
build(c, d, sd7, {"direction_mode": "auto"})
sl7 = [p for p in c.prot if (p.get("trigger") or {}).get("rule") == 1]
check(f"⑦ 空头 SL 覆盖 3+{_TOTAL_LOTS:g}={_m.ceil(3.0 + _TOTAL_LOTS - 1e-9)}",
      bool(sl7) and abs(float(sl7[-1]["initial"]["size"])) == float(_need6),
      str(sl7[-1]["initial"]["size"]) if sl7 else "无")
Path(d).unlink(missing_ok=True)

# ⑧ 空头 + 已有不足尺寸 SL(-3) → 重挂 32 并撤旧的
sd8 = tempfile.mkdtemp()
d = db2(close4h=EMA200 * 0.978)
c = Client(pos=3.0, side="short", sl_size=-3)
build(c, d, sd8, {"direction_mode": "auto"})
sl8 = [p for p in c.prot if (p.get("trigger") or {}).get("rule") == 1]
check("⑧ 空头补覆盖后只剩一条 SL", len(sl8) == 1, f"{len(sl8)} 条")
check(f"⑧ 空头补覆盖 size={_need6}", bool(sl8) and abs(float(sl8[-1]["initial"]["size"])) == float(_need6),
      str(sl8[-1]["initial"]["size"]) if sl8 else "无")
Path(d).unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("B4 · 反向持仓护栏（翻向时不许按新方向铺梯 + 挂错 SL）")
print("=" * 72)

# ① 持空 3 张 + 4H 涨破判多 → blocked，不铺新梯，补「上方 rule=1」保护空头
d = db2(close4h=EMA200 * 1.01)
c = Client(pos=3.0, side="short")
_, det, errs = build(c, d, sd, {"direction_mode": "auto"})
check("① 翻向被拦截", det.get("blocked") == "reverse_position", str(det.get("blocked")))
check("① 不铺新入场单", len(c.book) == 0, f"{len(c.book)} 条")
sl = [p for p in c.prot if (p.get("trigger") or {}).get("rule") == 1]
check("① 补了空头的上方 SL", bool(sl), f"{len(sl)} 条 rule=1")
if sl:
    check("① SL 在现价上方(空头真止损侧)",
          float(sl[-1]["trigger"]["price"]) > c.mark,
          f"sl={sl[-1]['trigger']['price']} mark={c.mark}")
    check("① SL size 为正", float(sl[-1]["initial"]["size"]) > 0,
          str(sl[-1]["initial"]["size"]))
check("① 无错误", not errs, str(errs[:1]))
Path(d).unlink(missing_ok=True)

# ② 持多 3 张 + 4H 跌破判空 → 镜像
d = db2(close4h=EMA200 * 0.978)
c = Client(pos=3.0, side="long")
_, det, _ = build(c, d, sd, {"direction_mode": "auto"})
check("② 镜像拦截", det.get("blocked") == "reverse_position", str(det.get("blocked")))
check("② 不铺新入场单", len(c.book) == 0, f"{len(c.book)}")
sl2 = [p for p in c.prot if (p.get("trigger") or {}).get("rule") == 2]
check("② 补了多头的下方 SL", bool(sl2), f"{len(sl2)} 条 rule=2")
if sl2:
    check("② SL 在现价下方", float(sl2[-1]["trigger"]["price"]) < c.mark,
          f"sl={sl2[-1]['trigger']['price']}")
Path(d).unlink(missing_ok=True)

# ③ 同向（持空 + 判空）→ 不许拦截，正常铺满
d = db2(close4h=EMA200 * 0.978)
c = Client(pos=3.0, side="short")
_, det, _ = build(c, d, sd, {"direction_mode": "auto"})
check("③ 同向不拦截", det.get("blocked") is None, str(det.get("blocked")))
check("③ 同向铺满 10 层", len(c.book) == 10, f"{len(c.book)}")
Path(d).unlink(missing_ok=True)

# ④ 无持仓 → 不许拦截
d = db2(close4h=EMA200 * 1.01)
c = Client(pos=0.0)
_, det, _ = build(c, d, sd, {"direction_mode": "auto"})
check("④ 无持仓不拦截", det.get("blocked") is None and len(c.book) == 10,
      f"blocked={det.get('blocked')} n={len(c.book)}")
Path(d).unlink(missing_ok=True)

# ⑤ int64 场景：size=0 但 mode=dual_short + value 反算出 0.1 张 → 必须识别为 short
d = db2(close4h=EMA200 * 1.01)
c = Client(pos=0.0)
c.get_positions = lambda: [{"contract": "ETH_USDT", "size": "0", "mode": "dual_short",
                            "value": "-2.33", "mark_price": "2333",
                            "entry_price": "2333"}]
_, det, _ = build(c, d, sd, {"direction_mode": "auto"})
check("⑤ size=0(int64截断) 仍识别为空头",
      det.get("blocked") == "reverse_position"
      and (det.get("position") or {}).get("side") == "short",
      str(det.get("position")))
check("⑤ 反算张数≈0.1", abs(((det.get("position") or {}).get("size") or 0) - 0.1) < 0.01,
      str((det.get("position") or {}).get("size")))
Path(d).unlink(missing_ok=True)

# ⑥ rescue SL 必须覆盖「持仓 + 同向挂单」，否则旧梯成交后又裸
d = db2(close4h=EMA200 * 1.01)
c = Client(pos=3.0, side="short")
for i in range(5):
    c.book.append({"id": f"x{i}", "price": 2400 + i * 5, "size": -1})
_, det, _ = build(c, d, sd, {"direction_mode": "auto"})
prot = det.get("protection") or {}
check("⑥ rescue 覆盖 持仓3+挂单5=8", abs(float(prot.get("size") or 0) - 8.0) < 0.01,
      f"cover={prot.get('size')} position={prot.get('position')}")
Path(d).unlink(missing_ok=True)


print()
print("=" * 72)
ok = sum(RESULTS)
print(f"总计：{ok}/{len(RESULTS)} 项通过")
print("=" * 72)
sys.exit(0 if ok == len(RESULTS) else 1)
