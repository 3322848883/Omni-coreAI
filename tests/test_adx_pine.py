"""ADX/DI：两套算法必须各自钉死，且 DI 不能互相串味。

背景（2026-11-02）：TV 上流传最广的 ADX 脚本（BeikabuOyaji「ADX and DI for v4」）
把 ADX 定义成 `ta.sma(DX, len)`，而 TV 内置 `ta.dmi` / 默认 ADX 指标用 `RMA(DX, len)`。
两者 DI 一样、ADX 不一样 —— 实测 ETH 15m 末 200 根：均值差 3.52、最大 12.60。
本项目 `adx()` 走 Wilder（= 内置），`adx_pine()` 走 SMA（= 该脚本）。
这个文件把「谁是哪个」「差多少」「名字怎么取」三件事都钉住，
防止以后有人"顺手统一"把其中一条改掉而无人发现。
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omnialpha.strategist.indicators import (  # noqa: E402
    adx,
    adx_pine,
    attach_indicators,
    latest_indicators,
    parse_indicator_name,
    sma,
)


def _walk(n=400, base=100.0, seed=5, drift=0.0, vol=1.0):
    rng = random.Random(seed)
    h, l, c = [], [], []
    px = base
    for _ in range(n):
        px += rng.gauss(drift, vol)
        o = px
        h.append(o + abs(rng.gauss(0, vol * 0.7)))
        l.append(o - abs(rng.gauss(0, vol * 0.7)))
        c.append(o)
    return h, l, c


# --------------------------------------------------------------------------
# 参考实现：逐行翻译用户给的 Pine v6 源码（不复用被测函数的任何中间步骤）
# --------------------------------------------------------------------------
def ref_pine_adx(h, l, c, length=14):
    n = len(c)
    tr, dmp, dmm = [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = h[i] - l[i]
            continue
        pc, ph, pl = c[i - 1], h[i - 1], l[i - 1]
        tr[i] = max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc))
        up = h[i] - ph
        dn = pl - l[i]
        dmp[i] = max(up, 0.0) if up > dn else 0.0
        dmm[i] = max(dn, 0.0) if dn > up else 0.0

    st = sp = sm = 0.0
    dip, dim, dx = [None] * n, [None] * n, [None] * n
    for i in range(n):
        st = st - st / length + tr[i]
        sp = sp - sp / length + dmp[i]
        sm = sm - sm / length + dmm[i]
        if st == 0:
            continue
        dip[i] = 100.0 * sp / st
        dim[i] = 100.0 * sm / st
        tot = dip[i] + dim[i]
        dx[i] = 0.0 if tot == 0 else 100.0 * abs(dip[i] - dim[i]) / tot

    idx = [i for i, v in enumerate(dx) if v is not None]
    ser = [dx[i] for i in idx]
    adx_line = [None] * n
    if len(ser) >= length:
        for j, v in enumerate(sma(ser, length)):
            adx_line[idx[j]] = v
    return {"adx": adx_line, "plus_di": dip, "minus_di": dim}


def _cmp(a, b, key, tol=1e-9):
    for i in range(len(a[key])):
        x, y = a[key][i], b[key][i]
        if y is None:
            assert x is None, f"{key}[{i}] 应为 None，实得 {x}"
            continue
        assert x is not None, f"{key}[{i}] 不应为 None"
        assert abs(x - y) < tol, f"{key}[{i}] {x} != {y}"


class TestPineFidelity(unittest.TestCase):
    def test_matches_reference_chop(self):
        h, l, c = _walk(400, seed=41, vol=1.5)
        _cmp(adx_pine(h, l, c, 14), ref_pine_adx(h, l, c, 14), "adx")
        _cmp(adx_pine(h, l, c, 14), ref_pine_adx(h, l, c, 14), "plus_di")
        _cmp(adx_pine(h, l, c, 14), ref_pine_adx(h, l, c, 14), "minus_di")

    def test_matches_reference_trend(self):
        h, l, c = _walk(400, seed=42, drift=0.45, vol=1.0)
        _cmp(adx_pine(h, l, c, 14), ref_pine_adx(h, l, c, 14), "adx")

    def test_matches_reference_other_periods(self):
        h, l, c = _walk(400, seed=43, vol=2.0)
        for p in (7, 10, 20, 30):
            _cmp(adx_pine(h, l, c, p), ref_pine_adx(h, l, c, p), "adx")

    def test_short_series(self):
        # Pine 版 DI 从第一根就有值（Wilder 从 0 递归，无需 sma 播种），
        # 所以 20 根就能出 ADX；只有不足 period 根时 ADX 才为 None。
        h, l, c = _walk(20, seed=44, vol=1.0)
        r = adx_pine(h, l, c, 14)
        self.assertTrue(any(v is not None for v in r["plus_di"]))
        self.assertTrue(any(v is not None for v in r["adx"]))
        h2, l2, c2 = _walk(10, seed=44, vol=1.0)
        r2 = adx_pine(h2, l2, c2, 14)
        self.assertTrue(all(v is None for v in r2["adx"]))


class TestTwoAlgorithmsDiffer(unittest.TestCase):
    """两条曲线必须**不同**，且差异在已知量级 —— 若某天被"统一"了，这里会红。"""

    def test_di_agrees_adx_disagrees(self):
        h, l, c = _walk(600, seed=45, vol=1.3)
        w = adx(h, l, c, 14)
        p = adx_pine(h, l, c, 14)
        for key in ("plus_di", "minus_di"):
            ds = [abs(w[key][i] - p[key][i]) for i in range(len(c))
                  if w[key][i] is not None and p[key][i] is not None]
            self.assertTrue(ds)
            self.assertLess(max(ds), 6.0, f"{key} 两版应基本一致，实得最大差 {max(ds)}")
        ds = [abs(w["adx"][i] - p["adx"][i]) for i in range(len(c))
              if w["adx"][i] is not None and p["adx"][i] is not None]
        self.assertTrue(ds)
        self.assertGreater(max(ds), 1.0, "ADX 两版算法不同，不该几乎相等（是否被误统一？）")
        self.assertLess(max(ds), 25.0, "ADX 差异过大，可能是实现炸了")

    def test_adx_in_valid_range(self):
        h, l, c = _walk(400, seed=46, vol=1.2)
        for fn in (adx, adx_pine):
            r = fn(h, l, c, 14)
            for key in ("adx", "plus_di", "minus_di"):
                vals = [v for v in r[key] if v is not None]
                self.assertTrue(vals)
                self.assertTrue(all(0 <= v <= 100 for v in vals), f"{fn.__name__}.{key} 越界")

    def test_dx_zero_when_no_direction(self):
        # 完全无波动：DI+ = DI- = 0 → DX = 0，不应 NaN
        n = 60
        c = [100.0] * n
        h = [100.5] * n
        l = [99.5] * n
        r = adx_pine(h, l, c, 14)
        vals = [v for v in r["adx"] if v is not None]
        self.assertTrue(all(v == 0.0 for v in vals), vals[:5])


class TestNames(unittest.TestCase):
    def test_sma_is_default(self):
        """2026-11-02 起默认 = BeikabuOyaji 的 SMA 版（TV 截图三点核对过）。"""
        for name in ("adx14", "adx20", "adx", "adx14_sma", "adx_sma"):
            spec = parse_indicator_name(name)
            self.assertEqual(spec["kind"], "adx_pine", name)
            self.assertEqual(spec["field"], "adx", name)

    def test_wilder_is_opt_in(self):
        for name in ("adx14_wilder", "adx_wilder"):
            spec = parse_indicator_name(name)
            self.assertEqual(spec["kind"], "adx", name)
            self.assertEqual(spec["field"], "adx", name)

    def test_di_names(self):
        for name, fld in (
            ("di_plus14", "plus_di"), ("di_minus14", "minus_di"),
            ("di_plus", "plus_di"), ("di_minus", "minus_di"),
            ("adx14_di_plus", "plus_di"), ("adx14_di_minus", "minus_di"),
            ("adx14_plus", "plus_di"), ("adx14_minus", "minus_di"),
            ("plus_di14", "plus_di"), ("minus_di14", "minus_di"),
        ):
            spec = parse_indicator_name(name)
            self.assertEqual(spec["field"], fld, name)

    def test_dir_is_not_di(self):
        """adx14_dir 容易被当成 DI-，必须拒掉而不是静默给个错值。"""
        with self.assertRaises(Exception):
            parse_indicator_name("adx14_dir")


class TestAttach(unittest.TestCase):
    def test_attach_both_versions(self):
        h, l, c = _walk(200, seed=47, drift=0.3)
        rows = [{"t": i, "o": c[i], "h": h[i], "l": l[i], "c": c[i], "v": 1.0}
                for i in range(len(c))]
        wanted = ["adx14", "adx14_wilder", "adx14_sma", "di_plus14", "di_minus14"]
        attach_indicators(rows, wanted)
        last = latest_indicators(rows, wanted)
        for k in wanted:
            self.assertIsNotNone(last[k], k)
        # 默认与 _sma 必须同值（同一实现），与 _wilder 必须不同
        self.assertAlmostEqual(last["adx14"], last["adx14_sma"], places=9)
        self.assertNotAlmostEqual(last["adx14"], last["adx14_wilder"], places=6)


if __name__ == "__main__":
    unittest.main()
