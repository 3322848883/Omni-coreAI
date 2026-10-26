# -*- coding: utf-8 -*-
"""方案 3：把 give_back 锁盈位挂成**交易所常驻触发单**的验证。

为什么（背景）：`give_back` 原本只有 60 秒扫描一条路径 ——
  - 两次扫描之间价格穿过锁盈位 → 引擎根本没看见（采样盲区）
  - 进程死亡/卡住 → 整段失明（上线期实测过一次 19 分钟空档）
触发单驻留在交易所，触发不依赖本进程 → 锁盈延迟归零，bot 死了也照常平仓。

本文件验的是「真能挂、挂得对、会自己挪、触发真能平」：
  - 5 个**不同币种**（BTC/ETH/SOL/DOGE/XRP），不同 quanto/价格步长/张数步长
  - 不同 `give_back_pct`（25/30/40/50/60）、多空双向
  - 全部跑真正的 `PaperExchange`：条件单要过引擎校验，触发要过撮合
  - 另加 wire 契约测试（Gate 嵌套 body）：张数必须 int、rule/price_type 必须对、
    棘轮必须「先挂新后撤旧」
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor              # noqa: E402
from omnialpha.paper.exchange import PaperExchange   # noqa: E402


# ── 行情替身：多币种、价格可控、每币独立合约规格 ─────────────────────
class _Meta:
    def __init__(self, quanto: float, tick: float, step: float,
                 min_notional: float = 1.0, lev_max: int = 100):
        self.quanto_multiplier = quanto
        self.order_price_round = tick
        self.size_step = step
        self.min_notional_usd = min_notional
        self.leverage_max = lev_max


SPECS = {
    # 币种：乘数 / 价格步长 / 张数步长 / 初始价
    "BTC_USDT": (_Meta(0.0001, 0.1, 1.0), 83000.0),
    "ETH_USDT": (_Meta(0.01, 0.01, 0.1), 2340.0),      # 小数张（0.1 步长）
    "SOL_USDT": (_Meta(1.0, 0.001, 1.0), 150.0),
    "DOGE_USDT": (_Meta(1.0, 0.00001, 1.0), 0.2),      # 极小 tick
    "XRP_USDT": (_Meta(1.0, 0.0001, 1.0), 2.5),
}


class MultiFeed:
    """最小行情源：按 symbol 返回价格与规格，不联网。"""

    name = "fake"

    def __init__(self):
        self.meta = {s: m for s, (m, _) in SPECS.items()}
        self.px = {s: p for s, (_, p) in SPECS.items()}
        self.funding_rate = 0.0001

    def set(self, symbol: str, px: float) -> None:
        self.px[symbol] = float(px)

    def get_last_price(self, symbol):
        return self.px[symbol]

    def get_orderbook_top(self, symbol, limit=5):
        p = self.px[symbol]
        return {"bids": [{"p": p, "s": 1e9}], "asks": [{"p": p, "s": 1e9}]}

    def get_ticker(self, symbol):
        p = self.px[symbol]
        return {"last": p, "mark_price": p, "funding_rate": self.funding_rate}

    def get_contract(self, symbol):
        return self.meta[symbol]

    def get_klines(self, symbol, interval, limit=100):
        return []

    def get_contract_stats(self, symbol, limit=1, interval=""):
        return []


def _flat(po: dict) -> dict:
    """统一两种返回形态：Gate 嵌套 / paper 平铺。"""
    init = po.get("initial") or {}
    trig = po.get("trigger") or {}
    return {
        "id": str(po.get("id") or po.get("order_id") or ""),
        "text": str(init.get("text") or po.get("text") or ""),
        "size": float(init.get("size") if init.get("size") is not None
                      else po.get("size") or 0),
        "reduce_only": bool(init.get("is_reduce_only") or init.get("reduce_only")
                            or po.get("reduce_only")),
        "trigger": float(trig.get("price") or po.get("trigger_price") or 0),
        "rule": int(trig.get("rule") if trig.get("rule") is not None
                    else po.get("rule") or 0),
        "price_type": str(trig.get("price_type") if trig.get("price_type") is not None
                          else po.get("trigger_price_type") or ""),
    }


class _Base(unittest.TestCase):
    LABEL = "gbt"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.feed = MultiFeed()
        self.ex = PaperExchange(
            env="paper",
            store_path=self.tmp / "data" / "bots" / "gbt" / "paper" / "account.db",
            feed=self.feed,
            config={"initial_capital": 100000, "leverage": 20},
        )
        self.executor = Executor(
            self.ex, label_prefix=self.LABEL, root=self.tmp, bot_id="gbt",
            account_risk={"give_back": "live"},
        )

    def tearDown(self):
        self.ex.store.close()
        self._td.cleanup()

    # ── 工具 ──
    def open_pos(self, symbol: str, size: float) -> float:
        o = self.ex.place_order({"contract": symbol, "size": size, "type": "market"})
        self.assertIn(str(o.get("status")), ("filled", "finished"), o)
        for p in self.ex.get_positions():
            if p.get("contract") == symbol:
                return float(p.get("entry_price") or 0)
        self.fail(f"{symbol} 未开仓成功")

    def put_sl(self, symbol: str, side: str, trigger: float, size: float) -> None:
        signed = -abs(size) if side == "long" else abs(size)
        self.ex.place_price_order({
            "initial": {"contract": symbol, "size": signed, "price": "0", "tif": "ioc",
                        "reduce_only": True, "text": f"t-{self.LABEL}-sl"},
            "trigger": {"rule": 2 if side == "long" else 1, "price_type": 1,
                        "price": str(trigger)},
        })

    def gb_orders(self, symbol: str) -> list[dict]:
        return [_flat(p) for p in (self.ex.list_price_orders(symbol) or [])
                if _flat(p)["text"].endswith("-gb")]

    def pos_size(self, symbol: str) -> float:
        for p in self.ex.get_positions():
            if p.get("contract") == symbol:
                return float(p.get("size") or 0)
        return 0.0


# ══════════════════════════════════════════════════════════════════
# 5 个币种 / 5 个 pct / 多空双向 / 5 种行为
# ══════════════════════════════════════════════════════════════════
class TestFiveSymbols(_Base):
    def test_1_btc_short_40pct_places_mark_trigger(self):
        """BTC 空单 / pct=40：挂涨破(mark)的减仓触发单，价位=入场−60%峰值利润。"""
        SYM = "BTC_USDT"
        entry = self.open_pos(SYM, -40)                  # 空 40 张 @83000
        self.put_sl(SYM, "short", 83440, 40)             # R = 440×40×0.0001 = 1.76
        self.executor.remember_give_back(SYM, "short", 40)
        self.feed.set(SYM, 82560)                        # cur = 1.76 = 1R → 武装
        out = self.executor.check_give_back(SYM)

        self.assertTrue(out["armed"], out)
        gb = out["gb"]
        self.assertEqual(gb["action"], "placed", gb)
        # level = 83000 − (1.76×0.6)/0.004 = 83000 − 264 = 82736
        self.assertAlmostEqual(gb["level"], 82736.0, places=2)

        rows = self.gb_orders(SYM)
        self.assertEqual(len(rows), 1, rows)
        r = rows[0]
        self.assertTrue(r["reduce_only"], "必须是 reduce-only")
        self.assertEqual(r["rule"], 1, "空单锁盈应涨破触发 rule=1")
        self.assertEqual(r["price_type"], "mark", "触发基准必须是 mark（与算价同源）")
        self.assertAlmostEqual(r["trigger"], 82736.0, places=2)
        self.assertEqual(r["size"], 40, "买平空 → 正张数，覆盖全仓")
        self.assertGreater(r["trigger"], self.feed.px[SYM], "触发价必须在现价上方")
        self.assertEqual(entry, 83000.0)

    def test_2_eth_long_30pct_fractional_size_floored(self):
        """ETH 多单 / pct=30 / 小数张（21.4）：方向反、rule=2、张数向零取整。"""
        SYM = "ETH_USDT"
        self.open_pos(SYM, 21.4)                          # 多 21.4 张 @2340
        self.put_sl(SYM, "long", 2295, 21.4)              # R = 45×21.4×0.01 = 9.63
        self.executor.remember_give_back(SYM, "long", 30)
        self.feed.set(SYM, 2400)                          # cur = 12.84 > 1R
        out = self.executor.check_give_back(SYM)

        gb = out["gb"]
        self.assertEqual(gb["action"], "placed", gb)
        # trigger = 12.84×0.7 = 8.988 → delta = 8.988/0.214 = 42 → level 2382
        self.assertAlmostEqual(gb["level"], 2382.0, places=2)
        r = self.gb_orders(SYM)[0]
        self.assertEqual(r["rule"], 2, "多单锁盈应跌破触发 rule=2")
        self.assertEqual(r["size"], -21, "price_orders 只收整数张：21.4 → 21（卖平多=负）")
        self.assertLess(r["trigger"], self.feed.px[SYM], "触发价必须在现价下方")

    def test_3_sol_short_60pct_ratchets_down_and_replaces(self):
        """SOL 空单 / pct=60：峰值创新低 → 触发价下移（锁更多），先挂新后撤旧。"""
        SYM = "SOL_USDT"
        self.open_pos(SYM, -100)                          # 空 100 张 @150
        self.put_sl(SYM, "short", 155, 100)               # R = 5×100 = 500
        self.executor.remember_give_back(SYM, "short", 60)
        self.feed.set(SYM, 145)                           # cur 500 = 1R
        first = self.executor.check_give_back(SYM)["gb"]
        self.assertEqual(first["action"], "placed", first)
        self.assertAlmostEqual(first["level"], 148.0, places=3)   # 150 − 200/100
        old_ids = {r["id"] for r in self.gb_orders(SYM)}

        self.feed.set(SYM, 143)                           # cur 700 → peak 上移
        moved = self.executor.check_give_back(SYM)["gb"]
        self.assertEqual(moved["action"], "moved", moved)
        self.assertAlmostEqual(moved["level"], 147.2, places=3)   # 150 − 280/100
        rows = self.gb_orders(SYM)
        self.assertEqual(len(rows), 1, f"棘轮后只能剩一条: {rows}")
        self.assertAlmostEqual(rows[0]["trigger"], 147.2, places=3)
        self.assertNotIn(rows[0]["id"], old_ids, "旧单必须被撤掉")

    def test_4_doge_long_50pct_is_idempotent(self):
        """DOGE 多单 / pct=50 / 极小 tick：同一峰值重复扫描不重挂（零 churn）。"""
        SYM = "DOGE_USDT"
        self.open_pos(SYM, 5000)                          # 多 5000 张 @0.2
        self.put_sl(SYM, "long", 0.198, 5000)             # R = 0.002×5000 = 10
        self.executor.remember_give_back(SYM, "long", 50)
        self.feed.set(SYM, 0.202)                         # cur 10 = 1R
        a = self.executor.check_give_back(SYM)["gb"]
        self.assertEqual(a["action"], "placed", a)
        self.assertAlmostEqual(a["level"], 0.201, places=6)   # 0.2 + 5/5000
        id1 = self.gb_orders(SYM)[0]["id"]

        b = self.executor.check_give_back(SYM)["gb"]
        self.assertEqual(b["action"], "kept", b)
        rows = self.gb_orders(SYM)
        self.assertEqual(len(rows), 1, "不得重复挂")
        self.assertEqual(rows[0]["id"], id1)

    def test_5_xrp_short_25pct_triggers_on_exchange_and_cleans_up(self):
        """XRP 空单 / pct=25：价格涨破锁盈价 → **交易所触发**平仓 → 状态与单据清理。"""
        SYM = "XRP_USDT"
        self.open_pos(SYM, -1000)                         # 空 1000 张 @2.5
        self.put_sl(SYM, "short", 2.6, 1000)              # R = 0.1×1000 = 100
        self.executor.remember_give_back(SYM, "short", 25)
        self.feed.set(SYM, 2.3)                           # cur 200 = 2R
        gb = self.executor.check_give_back(SYM)["gb"]
        self.assertEqual(gb["action"], "placed", gb)
        self.assertAlmostEqual(gb["level"], 2.35, places=4)   # 2.5 − 150/1000
        self.assertEqual(self.pos_size(SYM), -1000)

        # 价格涨破 2.35 → 交易所侧触发（**不经过 executor**，模拟 bot 睡着了）
        self.feed.set(SYM, 2.36)
        fired = self.ex.engine.scan_price_orders()
        self.assertTrue(any("gb" in str(_flat(f).get("text", "") or f) for f in fired),
                        f"锁盈触发单应被触发: {fired}")
        self.assertEqual(self.pos_size(SYM), 0, "触发后仓位应归零")
        self.assertEqual(self.gb_orders(SYM), [], "触发的单不该再留在盘上")

        # 下一轮扫描：无仓 → 清状态 + 无残留
        out = self.executor.check_give_back(SYM)
        self.assertEqual(out["skipped"], "no_position")
        self.assertEqual(self.executor._read_give_back(), {})


# ══════════════════════════════════════════════════════════════════
# 边界与协同（仍然走真 paper 交易所）
# ══════════════════════════════════════════════════════════════════
class TestEdges(_Base):
    def _armed_btc(self):
        SYM = "BTC_USDT"
        self.open_pos(SYM, -40)
        self.put_sl(SYM, "short", 83440, 40)
        self.executor.remember_give_back(SYM, "short", 40)
        self.feed.set(SYM, 82560)
        return SYM

    def test_not_armed_places_nothing(self):
        """峰值不到 1R 不挂：锁的利润不够付手续费。"""
        SYM = "BTC_USDT"
        self.open_pos(SYM, -40)
        self.put_sl(SYM, "short", 83440, 40)
        self.executor.remember_give_back(SYM, "short", 40)
        self.feed.set(SYM, 82960)                     # cur = 0.16 ≈ 0.09R
        out = self.executor.check_give_back(SYM)
        self.assertEqual(out["skipped"], "not_armed")
        self.assertEqual(self.gb_orders(SYM), [])

    def test_dry_places_nothing_but_reports_level(self):
        SYM = self._armed_btc()
        out = self.executor.check_give_back(SYM, dry=True)
        self.assertEqual(out["gb"]["action"], "dry")
        self.assertAlmostEqual(out["gb"]["level"], 82736.0, places=2)
        self.assertEqual(self.gb_orders(SYM), [], "dry 模式绝不能下单")

    def test_ai_cancel_price_all_does_not_kill_gb(self):
        """AI 的 `cancel_price_all` 是钝动作 —— 常驻锁盈单必须被当成在役保护跳过。"""
        SYM = self._armed_btc()
        self.executor.check_give_back(SYM)
        self.assertEqual(len(self.gb_orders(SYM)), 1)

        res = self.executor._cancel_price_all(SYM, label=self.LABEL)
        self.assertTrue(res.ok, res)
        self.assertEqual(len(self.gb_orders(SYM)), 1, "锁盈单被撤了 → 保护退化")
        self.assertTrue(res.detail.get("skipped_live_protection"), res.detail)

    def test_retrace_past_level_closes_market_and_cancels_gb(self):
        """已越线（浮盈仍>0）：市价平仓兜底，且先撤常驻单 —— 不留双重平仓路径。"""
        SYM = self._armed_btc()
        self.executor.check_give_back(SYM)                 # 挂上 82736
        self.feed.set(SYM, 82900)                          # cur 0.4 < trigger 1.056
        out = self.executor.check_give_back(SYM)
        self.assertIn("closed", out, out)
        self.assertEqual(self.pos_size(SYM), 0, "应市价平掉")
        self.assertEqual(self.gb_orders(SYM), [], "平仓后不得残留锁盈单")

    def test_position_gone_cancels_leftover_gb(self):
        """仓没了 → 盘上的锁盈单必须回收（否则下次开仓会被旧价位偷袭）。"""
        SYM = self._armed_btc()
        self.executor.check_give_back(SYM)
        self.assertEqual(len(self.gb_orders(SYM)), 1)
        self.ex.place_order({"contract": SYM, "size": 40, "type": "market",
                             "reduce_only": True})
        self.assertEqual(self.pos_size(SYM), 0)
        out = self.executor.check_give_back(SYM)
        self.assertEqual(out["skipped"], "no_position")
        self.assertEqual(self.gb_orders(SYM), [])
        self.assertEqual(self.executor._read_give_back(), {})


# ══════════════════════════════════════════════════════════════════
# wire 契约：Gate 的 `/price_orders` 真实 body 形态（嵌套 + 整数张）
# ══════════════════════════════════════════════════════════════════
class _Meta0:
    quanto_multiplier = 0.01
    order_price_round = 0.01
    size_step = 0.1


class GateLikeClient:
    """按 Gate 语义返回**嵌套**结构，并记录调用顺序（验证「先挂新后撤旧」）。"""

    def __init__(self, mark: float, size: float = -21.4, sl: float = 2330.0):
        self.mark = mark
        self.size = size
        self.sl = sl
        self.events: list[tuple] = []
        self.rows: list[dict] = [{
            "id": "sl-1",
            "initial": {"text": "t-gbt-sl", "size": abs(size), "reduce_only": True},
            "trigger": {"price": str(sl), "rule": 1, "price_type": 1},
            "status": "open",
        }]
        self._n = 0

    def get_positions(self):
        return [{"contract": "ETH_USDT", "size": self.size,
                 "entry_price": 2340.0, "mode": ""}]

    def get_ticker(self, symbol):
        return {"mark_price": self.mark, "last": self.mark}

    def get_contract(self, symbol):
        return _Meta0()

    def list_price_orders(self, contract=None):
        return [dict(r) for r in self.rows]

    def place_price_order(self, body):
        self._n += 1
        oid = f"gb-{self._n}"
        self.events.append(("place", oid, body))
        self.rows.append({"id": oid, "initial": body["initial"],
                          "trigger": body["trigger"], "status": "open"})
        return {"id": oid, "status": "open"}

    def cancel_price_order(self, oid):
        self.events.append(("cancel", oid))
        for r in self.rows:
            if r["id"] == oid:
                r["status"] = "cancelled"
        self.rows = [r for r in self.rows if r["status"] != "cancelled"]
        return {"id": oid}

    def close_position(self, contract, side=None, size=0):
        self.events.append(("close", contract, side))
        return {"id": "c-1", "status": "finished"}


class TestWireContract(unittest.TestCase):
    def _ex(self, client, td):
        ex = Executor(client, label_prefix="gbt", root=Path(td), bot_id="gbt")
        ex.remember_give_back("ETH_USDT", "short", 30)
        return ex

    def test_body_is_int_size_and_mark_price_type(self):
        with tempfile.TemporaryDirectory() as t:
            c = GateLikeClient(mark=2320.0)          # 空 21.4 张 @2340，cur=4.28
            ex = self._ex(c, t)
            out = ex.check_give_back("ETH_USDT")
            self.assertEqual(out["gb"]["action"], "placed", out["gb"])
            kind, oid, body = [e for e in c.events if e[0] == "place"][0]
            init, trig = body["initial"], body["trigger"]
            self.assertIsInstance(init["size"], int, f"触发单张数必须是 int: {init['size']!r}")
            self.assertEqual(init["size"], 21, "向零取整")
            self.assertIs(init["reduce_only"], True)
            self.assertEqual(init["price"], "0", "触发后走市价成交")
            self.assertEqual(trig["price_type"], 1, "必须 mark（与算价同源）")
            self.assertEqual(trig["rule"], 1, "空单涨破")
            # R=10×0.214=2.14 → 武装；cur=4.28 → trigger=2.996 → delta=14 → 2326
            self.assertAlmostEqual(float(trig["price"]), 2326.0, places=2)
            self.assertTrue(init["text"].endswith("-gb"))

    def test_ratchet_places_before_cancel(self):
        with tempfile.TemporaryDirectory() as t:
            c = GateLikeClient(mark=2320.0)
            ex = self._ex(c, t)
            ex.check_give_back("ETH_USDT")            # 挂 2326
            c.events.clear()
            c.mark = 2310.0                            # 峰值抬升 → 价位下移（锁更多）
            out = ex.check_give_back("ETH_USDT")
            self.assertEqual(out["gb"]["action"], "moved", out["gb"])
            self.assertAlmostEqual(out["gb"]["level"], 2319.0, places=2)
            kinds = [e[0] for e in c.events]
            self.assertEqual(kinds[:2], ["place", "cancel"],
                             f"必须先挂新后撤旧（保护不空窗）: {kinds}")

    def test_no_duplicate_when_already_correct(self):
        with tempfile.TemporaryDirectory() as t:
            c = GateLikeClient(mark=2320.0)
            ex = self._ex(c, t)
            ex.check_give_back("ETH_USDT")
            c.events.clear()
            out = ex.check_give_back("ETH_USDT")
            self.assertEqual(out["gb"]["action"], "kept", out["gb"])
            self.assertEqual([e[0] for e in c.events], [], "同价位零 churn")


class TestOffSwitch(unittest.TestCase):
    """`give_back: off` 必须真的关（`"off"` 是字符串真值，曾会掉进 live 分支）。"""

    class _Bot:
        bot_id = "gb-off"
        label_prefix = "gbt"
        account = ""
        account_risk = {"give_back": "off"}
        manage_positions = True

        def __init__(self, client):
            self._client = client

        def create_client(self):
            return self._client

    def test_off_cancels_leftover_trigger_and_clears_state(self):
        import json as _json

        from omnialpha.watcher import ProjectPaths, _give_back_sweep

        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            state_dir = root / "data" / "bots" / "gb-off" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "give_back.json").write_text(_json.dumps(
                {"positions": {"ETH_USDT|short": {"pct": 40, "peak_pnl": 5.0,
                                                  "armed": True, "entry": 2340,
                                                  "size": 21, "ts": 1}}}),
                encoding="utf-8")
            c = GateLikeClient(mark=2320.0)
            c.rows.append({"id": "gb-9",
                           "initial": {"text": "t-gbt-gb", "size": 21,
                                       "reduce_only": True},
                           "trigger": {"price": "2326", "rule": 1, "price_type": 1},
                           "status": "open"})
            bot = self._Bot(c)
            out = _give_back_sweep(bot, ProjectPaths(root=root), {})

            self.assertEqual(out, 0, "off 时不得平仓/挂单")
            self.assertEqual([e for e in c.events if e[0] == "close"], [],
                             "off 绝不能动仓")
            texts = [_flat(r)["text"] for r in c.rows]
            self.assertNotIn("t-gbt-gb", texts, "残留锁盈单必须被回收")
            self.assertIn("t-gbt-sl", texts, "SL 不能受牵连")
            live = _json.loads((state_dir / "give_back.json").read_text(encoding="utf-8"))
            self.assertEqual(live["positions"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
