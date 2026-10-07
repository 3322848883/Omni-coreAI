"""峰值回撤保护：单持仓**价格峰值**回撤到 k×ATR 时上移 SL（防坐电梯）。

为什么需要它（架构盘点 S18）：
  - 止损锚定的是**挂单那一刻给定的价**，挂上去就不再动。价格涨上去再跌回来它
    管不了 —— 那正是「坐电梯」。
  - 交易所侧的移动止盈（`trail`）在本项目**搁置**（需资金密码，paper 侧直接
    不支持），所以「让交易所自己往上挪止损」这条路是关着的。
  - 账户级权益熔断（`equity_deviation_halt`）只挡新开仓，防不住浮盈回吐。

口径是**逐仓 + 价格峰值**，与入场价解耦：所以浮盈仓和浮亏仓一视同仁，
也不受出入金 / 其他 bot 已实现盈亏的干扰（账户权益口径的噪声源）。

真动手时沿用 `_modify_tp_sl` 的既定顺序：**先挂新、再撤旧** —— 挂失败时旧 SL
还在，仓位不会裸。
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import (  # noqa: E402
    Executor,
    _PEAK_TRAIL_STALE_SEC,
    _parse_symbol_positions,
)
from omnialpha.gate_client import GateApiError  # noqa: E402
from omnialpha.monitoring import TYPE_PEAK_TRAIL  # noqa: E402

# 合成 K 线：每根 TR = max(1.0, 0.5, 0.5) = 1.0，close = 100 → ATR% = 1.0%
# （`_atr_pct` 用 1h 周期，close 取最后一根）
_KLINES = [{"h": 100.5, "l": 99.5, "c": 100.0} for _ in range(20)]


class _Client:
    """够用的 Gate 桩：只实现 `check_peak_trail` 这条路径碰到的接口。"""

    def __init__(self, mark=2800.0, positions=None, sls=None, klines=None):
        self.env = "live"
        self.mark = float(mark)
        self.positions = list(positions or [])
        self.sls = list(sls or [])
        self.klines = list(_KLINES if klines is None else klines)
        self.calls: list[str] = []
        self.placed: list[dict] = []
        self.placed_ids: set[str] = set()
        self.cancelled: list[str] = []
        self.fail_place = False
        self._next = 100

    def banner(self):
        return ""

    def get_account(self):
        return {"total": "1000"}

    def get_positions(self):
        return self.positions

    def get_ticker(self, symbol):
        return {"mark_price": self.mark}

    def get_contract(self, symbol):
        class _M:
            order_price_round = 0.1
            price_tick = 0.1
            quanto_multiplier = 1.0
            order_size_min = 1
            min_notional_usd = 1.0
        return _M()

    def get_klines(self, symbol, tf, n):
        return self.klines[-n:]

    def get_position_mode(self):
        return "single"

    def list_price_orders(self, contract=None):
        return list(self.sls)

    def get_price_order(self, oid):
        if str(oid) in self.placed_ids:
            return {"id": str(oid), "status": "open"}
        raise GateApiError(f"price order {oid} not found")

    def place_price_order(self, body):
        if self.fail_place:
            raise GateApiError("place rejected")
        self._next += 1
        oid = str(self._next)
        self.placed_ids.add(oid)
        self.calls.append("place")
        self.placed.append(body)
        return {"id": oid, "status": "open",
                "initial": body.get("initial"), "trigger": body.get("trigger")}

    def cancel_price_order(self, pid):
        self.calls.append("cancel")
        self.cancelled.append(str(pid))
        self.sls = [s for s in self.sls if str(s["id"]) != str(pid)]
        return True


def _long_pos(size=10, entry=2700.0):
    return [{"contract": "ETH_USDT", "size": size, "mode": "single",
             "entry_price": entry, "mark_price": 2800.0}]


def _short_pos(size=10, entry=2900.0):
    return [{"contract": "ETH_USDT", "size": -size, "mode": "single",
             "entry_price": entry, "mark_price": 2700.0}]


def _sl(oid="1", price=2720.0, size=-10, text="t-b1-sl", status="open"):
    return {"id": oid, "status": status, "text": text,
            "initial": {"text": text, "size": size, "trigger_price": price},
            "trigger": {"price": str(price), "rule": 2}}


def _ex(client, root, **risk):
    base = {"peak_trail": True, "peak_trail_atr": 1.5}
    base.update(risk)
    return Executor(client, symbols_whitelist=["ETH_USDT"], root=root,
                    bot_id="b1", label_prefix="b1", require_sl=False,
                    account_risk=base)


def _state_path(root, account=None, bot_id="b1") -> Path:
    base = (root / "data" / "accounts" / account / "state" if account
            else root / "data" / "bots" / bot_id / "state")
    return base / "peak_trail.json"


def _seed(root, key, peak, entry, ts=None, account=None, bot_id="b1"):
    p = _state_path(root, account, bot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "positions": {key: {"peak": peak, "entry": entry, "size": 10,
                            "ts": time.time() if ts is None else ts}},
    }), encoding="utf-8")


def _peak_state(root, account=None, bot_id="b1") -> dict:
    p = _state_path(root, account, bot_id)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("positions") or {}


class TestSwitches(unittest.TestCase):
    def test_off_by_default(self):
        """没配 `peak_trail` → 完全不动，连状态文件都不写。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _Client(positions=_long_pos(), sls=[_sl()])
            ex = Executor(client, symbols_whitelist=["ETH_USDT"], root=root,
                          bot_id="b1", label_prefix="b1", require_sl=False)
            out = ex.check_peak_trail("ETH_USDT")
            self.assertEqual(out["peak_trail"], "off")
            self.assertEqual(client.calls, [])
            self.assertEqual(_peak_state(root), {})

    def test_zero_mult_disables(self):
        with tempfile.TemporaryDirectory() as td:
            client = _Client(positions=_long_pos(), sls=[_sl()])
            ex = _ex(client, Path(td), peak_trail_atr=0)
            self.assertEqual(ex.check_peak_trail("ETH_USDT")["peak_trail"], "off")
            self.assertEqual(client.calls, [])

    def test_missing_mult_disables(self):
        with tempfile.TemporaryDirectory() as td:
            client = _Client(positions=_long_pos(), sls=[_sl()])
            ex = _ex(client, Path(td), peak_trail_atr=None)
            self.assertEqual(ex.check_peak_trail("ETH_USDT")["peak_trail"], "off")


class TestPeakState(unittest.TestCase):
    def test_peak_only_rises_for_long(self):
        """多单峰值取最高价，只上不下。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _Client(positions=_long_pos(), sls=[_sl()])
            ex = _ex(client, root)
            for m in (2750.0, 2800.0, 2760.0):
                client.mark = m
                ex.check_peak_trail("ETH_USDT")
            self.assertEqual(_peak_state(root)["ETH_USDT|long"]["peak"], 2800.0)

    def test_peak_only_falls_for_short(self):
        """空单峰值取最低价，只下不上。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _Client(mark=2750.0, positions=_short_pos(),
                             sls=[_sl(price=2760.0, size=10)])
            ex = _ex(client, root)
            for m in (2750.0, 2700.0, 2740.0):
                client.mark = m
                ex.check_peak_trail("ETH_USDT")
            self.assertEqual(_peak_state(root)["ETH_USDT|short"]["peak"], 2700.0)

    def test_state_cleared_when_flat(self):
        """持仓没了必须清峰值 —— 否则下次开同一标的会拿旧高点卡新仓。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            ex = _ex(_Client(positions=[]), root)
            out = ex.check_peak_trail("ETH_USDT")
            self.assertEqual(out["skipped"], "no_position")
            self.assertEqual(_peak_state(root), {})

    def test_flat_does_not_touch_other_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            _seed(root, "BTC_USDT|long", peak=90000.0, entry=80000.0)
            _ex(_Client(positions=[]), root).check_peak_trail("ETH_USDT")
            self.assertEqual(list(_peak_state(root)), ["BTC_USDT|long"])

    def test_entry_change_resets_peak(self):
        """入场价变了 = 不是同一条持仓（平掉再开 / 加仓）→ 重新起算。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2500.0)
            client = _Client(mark=2760.0, positions=_long_pos(entry=2700.0),
                             sls=[_sl()])
            _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(_peak_state(root)["ETH_USDT|long"]["peak"], 2760.0)

    def test_stale_observation_resets_peak(self):
        """上次观测太久远（连续性断了）→ 历史高点不可信，重新起算。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0,
                  ts=time.time() - _PEAK_TRAIL_STALE_SEC - 60)
            client = _Client(mark=2760.0, positions=_long_pos(), sls=[_sl()])
            _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(_peak_state(root)["ETH_USDT|long"]["peak"], 2760.0)

    def test_side_flip_drops_old_direction(self):
        """方向反转 → 旧方向的峰值必须清掉，否则反手回来会复用它。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2700.0, positions=_short_pos(),
                             sls=[_sl(price=2760.0, size=10)])
            _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(list(_peak_state(root)), ["ETH_USDT|short"])

    def test_state_path_is_bot_scoped(self):
        """状态恒落 bot 级 —— 逐 bot 的开关配逐 bot 的状态，互不覆盖。

        账户级共享时，同账户下开了这个开关的 bot 会互相写坏对方的峰值记录。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _Client(positions=_long_pos(), sls=[_sl()])
            ex = Executor(client, symbols_whitelist=["ETH_USDT"], root=root,
                          bot_id="b1", label_prefix="b1", require_sl=False,
                          account="gate-main",
                          account_risk={"peak_trail": True, "peak_trail_atr": 1.5})
            ex.check_peak_trail("ETH_USDT")
            self.assertIn("ETH_USDT|long", _peak_state(root, bot_id="b1"))
            self.assertEqual(_peak_state(root, account="gate-main"), {},
                             "不再写账户级路径")


class TestNoActionPaths(unittest.TestCase):
    """三条不下单的红线。"""

    def test_no_owned_sl_skips(self):
        """没有本 bot 的 SL → 交给 ensure_protection，不在这里补。"""
        with tempfile.TemporaryDirectory() as td:
            client = _Client(positions=_long_pos(), sls=[])
            out = _ex(client, Path(td)).check_peak_trail("ETH_USDT")
            self.assertEqual(out["skipped"], "no_owned_sl")
            self.assertEqual(client.calls, [])

    def test_other_bot_sl_not_touched(self):
        """别人命名空间的 SL 不算 owned —— 不许替别的 bot 挪保护。"""
        with tempfile.TemporaryDirectory() as td:
            client = _Client(positions=_long_pos(), sls=[_sl(text="t-other-sl")])
            out = _ex(client, Path(td)).check_peak_trail("ETH_USDT")
            self.assertEqual(out["skipped"], "no_owned_sl")
            self.assertEqual(client.calls, [])

    def test_sl_not_better_skips(self):
        """目标不比现有 SL 更保守 → 不动手（SL 单调不降，不会来回抖）。"""
        with tempfile.TemporaryDirectory() as td:
            client = _Client(mark=2800.0, positions=_long_pos(),
                             sls=[_sl(price=2790.0)])
            out = _ex(client, Path(td)).check_peak_trail("ETH_USDT")
            self.assertEqual(out["skipped"], "sl_not_better")
            self.assertEqual(client.calls, [])

    def test_atr_growth_does_not_move_sl_down(self):
        """ATR 变大 → 目标下移 → 判成「不更好」而**不动**（保护只收紧不放松）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2800.0, positions=_long_pos(),
                             sls=[_sl(price=2750.0)],
                             klines=[{"h": 110.0, "l": 90.0, "c": 100.0}] * 20)
            out = _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(out["skipped"], "sl_not_better")
            self.assertEqual(client.cancelled, [])

    def test_trail_breached_skips_and_does_not_place(self):
        """回撤已经越过目标（目标落在 mark 非法侧）→ 只报不挂。

        此刻挂单会立刻触发，那是「市价平仓」—— 一个**不同**的动作，不该由
        「上移止损」这条路径偷偷做掉。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2600.0, positions=_long_pos(), sls=[_sl()])
            out = _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(out["skipped"], "trail_breached")
            self.assertGreater(out["target_sl"], out["mark"])
            self.assertEqual(client.calls, [])

    def test_dual_side_trails_each_leg(self):
        """双向持仓：两条腿各自跟踪峰值、各自上移自己的 SL。

        旧版在 `len(positions) > 1` 时直接 `ambiguous_side` 返回 —— 而「永远双向」
        的阶梯策略长期双向，等于这条保护对它完全失效。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _Client(positions=_long_pos() + _short_pos(),
                             sls=[_sl(oid="1", price=2720.0, size=-10),
                                  _sl(oid="2", price=2850.0, size=10)])
            out = _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(len(out["sides"]), 2)
            self.assertEqual({s["position_side"] for s in out["sides"]}, {"long", "short"})
            self.assertEqual(sorted(_peak_state(root)),
                             ["ETH_USDT|long", "ETH_USDT|short"],
                             "两条腿的峰值各存各的键")
            self.assertTrue(all(s.get("moved") for s in out["sides"]),
                            "两条腿都该上移自己的 SL")

    def test_dual_side_sl_matched_by_own_side(self):
        """回撤越过 SL 时也要认对腿 —— 靠保护单自身的 size 符号，不是它相对 mark 的位置。

        这一刻 SL 恰好跑到 mark 的另一侧，用位置判会把它认成对面那条腿的。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 峰值 2800、现价 2650（已跌破多单 SL 2720）→ 位置判会以为 2720 属于空腿
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2650.0, positions=_long_pos(),
                             sls=[_sl(oid="1", price=2720.0, size=-10)])
            out = _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(out["sides"][0].get("skipped"), "trail_breached",
                             "该腿的 SL 必须被认出来（否则会误报 no_owned_sl）")

    def test_no_atr_errors(self):
        with tempfile.TemporaryDirectory() as td:
            client = _Client(positions=_long_pos(), sls=[_sl()], klines=[])
            out = _ex(client, Path(td)).check_peak_trail("ETH_USDT")
            self.assertEqual(out["error"], "no atr")
            self.assertEqual(client.calls, [])

    def test_no_mark_errors(self):
        with tempfile.TemporaryDirectory() as td:
            client = _Client(mark=0.0, positions=_long_pos(), sls=[_sl()])
            out = _ex(client, Path(td)).check_peak_trail("ETH_USDT")
            self.assertEqual(out["error"], "no mark price")

    def test_dry_records_target_but_places_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2800.0, positions=_long_pos(), sls=[_sl()])
            out = _ex(client, root, peak_trail="dry").check_peak_trail("ETH_USDT")
            self.assertEqual(out["peak_trail"], "dry")
            self.assertEqual(out["target_sl"], 2758.0)   # 2800 − 2800×1.0%×1.5
            self.assertEqual(client.calls, [])
            self.assertNotIn("moved", out)


class TestMove(unittest.TestCase):
    def test_long_moves_sl_up_place_before_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2800.0, positions=_long_pos(), sls=[_sl()])
            out = _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(out["moved"]["from"], 2720.0)
            self.assertEqual(out["moved"]["to"], 2758.0)
            self.assertEqual(out["moved"]["size"], 10)
            self.assertEqual(client.cancelled, ["1"])
            # **先挂新、再撤旧**：挂失败时旧 SL 还在，仓位不会裸
            self.assertLess(client.calls.index("place"),
                            client.calls.index("cancel"))

    def test_new_sl_trigger_price_in_body(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2800.0, positions=_long_pos(), sls=[_sl()])
            _ex(client, root).check_peak_trail("ETH_USDT")
            body = client.placed[0]
            self.assertEqual(body["trigger"]["price"], "2758.0")
            self.assertEqual(body["trigger"]["rule"], 2)      # 跌破触发
            self.assertEqual(body["initial"]["size"], -10)    # 卖平多单
            self.assertTrue(body["initial"]["reduce_only"])

    def test_short_moves_sl_down(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|short", peak=2700.0, entry=2900.0)
            client = _Client(mark=2700.0, positions=_short_pos(),
                             sls=[_sl(price=2760.0, size=10)])
            out = _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(out["moved"]["from"], 2760.0)
            self.assertEqual(out["moved"]["to"], 2740.5)
            body = client.placed[0]
            self.assertEqual(body["trigger"]["rule"], 1)      # 涨破触发
            self.assertEqual(body["initial"]["size"], 10)     # 买平空单

    def test_place_failure_keeps_old_sl(self):
        """挂新失败 → 旧 SL 必须原封不动（这就是「先挂后撤」的全部意义）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2800.0, positions=_long_pos(), sls=[_sl()])
            client.fail_place = True
            out = _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertIn("replace failed", out["error"])
            self.assertEqual(client.cancelled, [])
            self.assertEqual([s["id"] for s in client.sls], ["1"], "旧 SL 不该被撤")

    def test_full_position_size_covered(self):
        """新 SL 按**持仓张数**挂（不是旧 SL 的张数）—— 覆盖不足会留半裸仓。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2800.0, positions=_long_pos(size=25),
                             sls=[_sl(size=-10)])
            out = _ex(client, root).check_peak_trail("ETH_USDT")
            self.assertEqual(out["moved"]["size"], 25)
            self.assertEqual(client.placed[0]["initial"]["size"], -25)

    def test_second_sweep_is_noop(self):
        """已挪到位 → 下一次扫描判成 sl_not_better，不会反复撤挂。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2800.0, positions=_long_pos(), sls=[_sl()])
            ex = _ex(client, root)
            ex.check_peak_trail("ETH_USDT")
            n = len(client.calls)
            client.sls = [_sl(oid="200", price=2758.0)]   # 假装新单已生效
            out = ex.check_peak_trail("ETH_USDT")
            self.assertEqual(out["skipped"], "sl_not_better")
            self.assertEqual(len(client.calls), n)

    def test_flat_after_move_clears_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed(root, "ETH_USDT|long", peak=2800.0, entry=2700.0)
            client = _Client(mark=2800.0, positions=_long_pos(), sls=[_sl()])
            ex = _ex(client, root)
            ex.check_peak_trail("ETH_USDT")
            client.positions = []
            ex.check_peak_trail("ETH_USDT")
            self.assertEqual(_peak_state(root), {})


class TestSweep(unittest.TestCase):
    """watcher 侧接线：`_peak_trail_sweep` 真的会挪、且异常/越界都有人知道。"""

    BOT = "pt-test"

    def _bot(self, **ar):
        from omnialpha.config import BotConfig

        return BotConfig(
            bot_id=self.BOT, env="paper", symbols=["ETH_USDT"],
            label_prefix="pt", account_risk=dict(ar),
        )

    def _paths(self, td):
        from omnialpha.watcher import ProjectPaths

        return ProjectPaths(root=Path(td))

    def _client(self, mark=2800.0, positions=None, sls=None, klines=None):
        return _Client(mark=mark, positions=positions,
                       sls=[_sl(text="t-pt-sl")] if sls is None else sls,
                       klines=klines)

    def _state(self, root):
        return _peak_state(root, bot_id=self.BOT)

    def _seed(self, root, key="ETH_USDT|long", peak=2800.0, entry=2700.0):
        _seed(root, key, peak, entry, bot_id=self.BOT)

    def test_sweep_moves_once_then_noop(self):
        from omnialpha.watcher import _peak_trail_sweep

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bot = self._bot(peak_trail=True, peak_trail_atr=1.5)
            client = self._client(positions=_long_pos())
            bot.create_client = lambda: client
            alerted: dict = {}
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), alerted), 1)
            self.assertEqual(len(client.placed), 1)
            self.assertTrue(self._state(root), "状态应落盘在 bot 目录")
            # 第二轮的 peak 已等于 mark、目标不比新 SL 更好 → 不再撤挂
            client.sls = [_sl(oid="300", price=2758.0, text="t-pt-sl")]
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), alerted), 0)
            self.assertEqual(len(client.placed), 1)

    def test_sweep_off_by_default(self):
        from omnialpha.watcher import _peak_trail_sweep

        with tempfile.TemporaryDirectory() as td:
            bot = self._bot()          # 没配 account_risk
            client = self._client(positions=_long_pos())
            bot.create_client = lambda: client
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), {}), 0)
            self.assertEqual(client.placed, [])

    def test_sweep_dry_places_nothing(self):
        from omnialpha.watcher import _peak_trail_sweep

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed(root)
            bot = self._bot(peak_trail="dry", peak_trail_atr=1.5)
            client = self._client(positions=_long_pos())
            bot.create_client = lambda: client
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), {}), 0)
            self.assertEqual(client.placed, [])

    def test_sweep_place_failure_alerts_and_rate_limits(self):
        from omnialpha.monitoring import read_alerts
        from omnialpha.watcher import _peak_trail_sweep

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed(root)
            bot = self._bot(peak_trail=True, peak_trail_atr=1.5)
            client = self._client(positions=_long_pos())
            client.fail_place = True
            bot.create_client = lambda: client
            alerted: dict = {}
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), alerted), 0)
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), alerted), 0)
            alerts = read_alerts(root, self.BOT, TYPE_PEAK_TRAIL)
            self.assertEqual(len(alerts), 1, "同一窗口内反复失败只告警一次")
            self.assertIn("旧 SL 仍在", alerts[0]["detail"])

    def test_sweep_breach_alerts(self):
        from omnialpha.monitoring import read_alerts
        from omnialpha.watcher import _peak_trail_sweep

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._seed(root)
            bot = self._bot(peak_trail=True, peak_trail_atr=1.5)
            client = self._client(mark=2600.0, positions=_long_pos())
            bot.create_client = lambda: client
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), {}), 0)
            alerts = read_alerts(root, self.BOT, TYPE_PEAK_TRAIL)
            self.assertEqual(len(alerts), 1)
            self.assertIn("越过目标", alerts[0]["detail"])
            self.assertEqual(client.placed, [])

    def test_sweep_skips_quietly_when_flat(self):
        """没持仓是常态，不该刷告警。"""
        from omnialpha.monitoring import read_alerts
        from omnialpha.watcher import _peak_trail_sweep

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bot = self._bot(peak_trail=True, peak_trail_atr=1.5)
            client = self._client(positions=[])
            bot.create_client = lambda: client
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), {}), 0)
            self.assertEqual(read_alerts(root, self.BOT, TYPE_PEAK_TRAIL), [])

    def test_sweep_survives_client_failure(self):
        from omnialpha.watcher import _peak_trail_sweep

        with tempfile.TemporaryDirectory() as td:
            bot = self._bot(peak_trail=True, peak_trail_atr=1.5)

            def _boom():
                raise GateApiError("no keys")

            bot.create_client = _boom
            self.assertEqual(_peak_trail_sweep(bot, self._paths(td), {}), 0)


class TestHelpers(unittest.TestCase):
    def test_owned_sl_orders_filters(self):
        with tempfile.TemporaryDirectory() as td:
            client = _Client(sls=[
                _sl(oid="1", price=2720.0),                       # 保留
                _sl(oid="2", price=2710.0, status="cancelled"),   # 已终结
                _sl(oid="3", price=2730.0, text="t-b1-tp"),       # 不是 SL
                _sl(oid="4", price=2740.0, text="t-other-sl"),    # 别人的
            ])
            ex = _ex(client, Path(td))
            self.assertEqual(ex._owned_sl_orders("ETH_USDT"), [("1", 2720.0, 10)])

    def test_parse_positions_exposes_entry_price(self):
        pos = _parse_symbol_positions(
            [{"contract": "ETH_USDT", "size": 10, "mode": "single",
              "entry_price": "2700.5", "mark_price": "2800.0"}], "ETH_USDT")
        self.assertEqual(pos[0]["entry_price"], 2700.5)
        self.assertEqual(pos[0]["mark_price"], 2800.0)
        self.assertEqual(pos[0]["side"], "long")

    def test_parse_positions_missing_entry_is_zero(self):
        """字段缺失 → 0.0（调用方按「判不了身份」保守处理，不当成有效入场价）。"""
        pos = _parse_symbol_positions(
            [{"contract": "ETH_USDT", "size": 10}], "ETH_USDT")
        self.assertEqual(pos[0]["entry_price"], 0.0)


if __name__ == "__main__":
    unittest.main()
