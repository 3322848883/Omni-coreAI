#!/usr/bin/env python3
"""在 migrate_v2 的 ohlc_sample 门禁里留痕最差样本（币/周期/列/两值）。"""
import pathlib

p = pathlib.Path("pa-data-source/migrate_v2.py")
s = p.read_text(encoding="utf-8")

old = """        for t, o, h, l, c in src_rows:
            dst = conn.execute(
                "SELECT o,h,l,c FROM kline_v2_new WHERE symbol=? AND interval=? AND t=?",
                (symbol, interval, t)).fetchone()
            if not dst:
                worst = float("inf")
                continue
            for a, b in zip((o, h, l, c), dst):
                samples += 1
                x, y = float(a), float(b)
                if x != y:
                    worst = max(worst, abs(x - y) / max(abs(x), abs(y), 1e-12))"""
new = """        for t, o, h, l, c in src_rows:
            dst = conn.execute(
                "SELECT o,h,l,c FROM kline_v2_new WHERE symbol=? AND interval=? AND t=?",
                (symbol, interval, t)).fetchone()
            if not dst:
                worst = float("inf")
                continue
            for col, a, b in zip(("o", "h", "l", "c"), (o, h, l, c), dst):
                samples += 1
                x, y = float(a), float(b)
                if x != y:
                    rel = abs(x - y) / max(abs(x), abs(y), 1e-12)
                    if rel > worst:
                        worst = rel
                        worst_detail.update({
                            "symbol": symbol, "interval": interval, "t": t,
                            "col": col, "src": repr(a), "dst": repr(b),
                            "src_type": type(a).__name__, "dst_type": type(b).__name__,
                        })"""
assert old in s
s = s.replace(old, new, 1)

s = s.replace("""    worst = 0.0
    samples = 0""",
"""    worst = 0.0
    samples = 0
    worst_detail: dict = {}""", 1)
s = s.replace('gates["ohlc_sample"] = {"ok": worst <= rel_tol, "worst_rel": worst, "samples": samples}',
              'gates["ohlc_sample"] = {"ok": worst <= rel_tol, "worst_rel": worst,\n'
              '                            "samples": samples, "worst_detail": worst_detail}')
p.write_text(s, encoding="utf-8")
print("gate detail added")
