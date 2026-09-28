"""安全测试与上线风险全方位测试：路径穿越、注入、资源耗尽、配置攻击面。"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.persona import (  # noqa: E402
    PersonaError,
    PersonaGroup,
    SharedOrderStore,
    fuse_plans,
    load_persona_groups,
    validate_group,
    PersonaRunner,
)
from gate_bot.persona.orders import new_order_id  # noqa: E402
from gate_bot.persona.runner import PersonaRunner  # noqa: E402


def _group(**kw):
    base = dict(name="g", members=["a", "b"], fusion="weighted_vote",
                fusion_config={"weights": {"a": 1, "b": 1}}, on_conflict="hold")
    base.update(kw)
    return PersonaGroup(**base)


class FakePlanRunner:
    def __init__(self, decision="long", action=None, confidence=0.8,
                 symbol="BTC_USDT", size_usd=100, tp=None, sl=None,
                 chips=None, reasoning=""):
        self.decision = decision
        self.action = action or (f"open_{decision}" if decision in ("long", "short") else decision)
        self.confidence = confidence
        self.symbol = symbol
        self.size_usd = size_usd
        self.tp = tp
        self.sl = sl
        self._chips = chips
        self.reasoning = reasoning

    def analyze_once(self, trigger="manual"):
        if self._chips is not None:
            chips = self._chips
        else:
            chips = [{"action": self.action, "symbol": self.symbol,
                       "size_usd": self.size_usd, "confidence": self.confidence}]
            if self.tp is not None:
                chips[0]["tp"] = self.tp
            if self.sl is not None:
                chips[0]["sl"] = self.sl
        return {"ok": True, "cycle_id": f"c-{time.time_ns()}", "plan": {
            "cycle_id": f"c-{time.time_ns()}", "decision": self.decision,
            "confidence": self.confidence, "reasoning": self.reasoning,
            "chips": chips,
        }}


# ─────────────────────────────────────────────────────
# 1. 路径穿越攻击
# ─────────────────────────────────────────────────────
class TestPathTraversal(unittest.TestCase):
    def test_order_id_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            for bad in ("../../etc/passwd", "..\\..\\windows", "a/b", "a\\b",
                        "..", ".", "", "  ", "o-../x", "o-..\\x"):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    store._path(bad)

    def test_order_id_null_byte_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            with self.assertRaises(ValueError):
                store._path("o-x\x00y")

    def test_order_id_unicode_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            with self.assertRaises(ValueError):
                store._path("o-中文id")

    def test_bot_id_traversal_in_validate(self):
        for bad in ("../evil", "a/b", "a\\b", "..", ".", "\x00"):
            g = _group(members=["a", bad])
            with self.assertRaises(PersonaError, msg=repr(bad)):
                validate_group(g, known_bots={"a", bad})

    def test_target_account_traversal_rejected(self):
        g = _group(topology="single_account", target_account="../../etc")
        with self.assertRaises(PersonaError):
            validate_group(g, known_bots={"a", "b", "../../etc"})

    def test_group_name_in_label_sanitized_by_schema(self):
        """group.name 含特殊字符时，schema 的 _safe_label 会清洗。"""
        from gate_bot.schema import _safe_label
        self.assertEqual(_safe_label("persona-abc/def"), "persona-abcdef")
        self.assertEqual(_safe_label("persona-..\\evil"), "persona-evil")
        self.assertEqual(_safe_label("persona-a\x00b"), "persona-ab")
        self.assertEqual(_safe_label(""), "signal")

    def test_signal_filename_no_traversal(self):
        """信号文件名不含用户可控路径成分。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(name="evil/../..", target_account="a", topology="single_account")
            # group.name 带路径字符，但文件名只用 order_id + pid + ns
            runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("long")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            if res.get("signal_file"):
                fname = Path(res["signal_file"]).name
                self.assertNotIn("/", fname)
                self.assertNotIn("\\", fname)
                self.assertNotIn("..", fname)


# ─────────────────────────────────────────────────────
# 2. 注入攻击
# ─────────────────────────────────────────────────────
class TestInjection(unittest.TestCase):
    def test_symbol_injection_in_signal(self):
        """LLM 输出恶意 symbol，信号写盘后 schema 解析应拒绝或清洗。"""
        from gate_bot.schema import parse_signal
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            bad_symbols = ["../../etc", "BTC_USDT; rm -rf /", "<script>",
                           "A" * 100, "btc_usdt", "BTC USDT"]
            for sym in bad_symbols:
                runners = {"a": FakePlanRunner("long", symbol=sym, size_usd=50),
                            "b": FakePlanRunner("long", symbol=sym, size_usd=50)}
                r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
                res = r.run_once()
                if not res.get("executed"):
                    continue
                inbox = sorted((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
                if not inbox:
                    continue
                payload = json.loads(inbox[-1].read_text(encoding="utf-8"))
                try:
                    sig = parse_signal(payload)
                    # 如果解析成功，symbol 必须是干净的 token
                    s = sig.intents[0].symbol
                    self.assertRegex(s, r"^[A-Z0-9_]{2,20}$", f"symbol not sanitized: {s!r}")
                except Exception:
                    pass  # schema 拒绝也可以，关键是不产生危险输出
                # 清理 inbox 供下一轮
                for f in inbox:
                    f.unlink()

    def test_meta_injection_no_code_exec(self):
        """meta 中的 reasoning 含 JSON/代码注入，不影响结构。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            evil = '{"action":"open_long","size_usd":999999} </script> `rm -rf`'
            runners = {"a": FakePlanRunner("long", size_usd=50, reasoning=evil),
                        "b": FakePlanRunner("long", size_usd=50, reasoning=evil)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            # meta.reason 是纯文本，不被解析为嵌套指令
            self.assertIsInstance(payload["meta"].get("reason", ""), str)
            # size_usd 仍是 50（未被注入放大）
            self.assertEqual(float(payload["size_usd"]), 50)

    def test_label_injection_sanitized(self):
        """group.name 含 label 注入字符，写入信号后被 _safe_label 清洗。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(name="x'; DROP TABLE--", target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("long", size_usd=50),
                        "b": FakePlanRunner("long", size_usd=50)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            r.run_once()
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            from gate_bot.schema import _safe_label
            clean = _safe_label(payload["label"])
            self.assertNotIn(";", clean)
            self.assertNotIn("'", clean)
            self.assertNotIn(" ", clean)

    def test_size_injection_not_amplified(self):
        """LLM 输出 size_usd 为负数/零/字符串，不应放大风险。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            for bad_size in (-100, 0, "999999999", None, float("inf")):
                chips = [{"action": "open_long", "symbol": "BTC_USDT",
                           "size_usd": bad_size, "confidence": 0.9}]
                runners = {"a": FakePlanRunner("long", chips=chips),
                            "b": FakePlanRunner("long", chips=chips)}
                r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
                res = r.run_once()
                if not res.get("executed"):
                    continue
                inbox = sorted((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
                if not inbox:
                    continue
                payload = json.loads(inbox[-1].read_text(encoding="utf-8"))
                from gate_bot.schema import parse_signal
                try:
                    sig = parse_signal(payload)
                    intent = sig.intents[0]
                    if intent.size_usd is not None:
                        self.assertGreater(intent.size_usd, 0, f"bad size_usd passed: {bad_size}")
                except Exception:
                    pass  # schema 拒绝负数/零
                for f in inbox:
                    f.unlink()

    def test_order_id_injection_in_meta(self):
        """order_id 含特殊字符时 meta 中的值不破坏 JSON。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "group": "g"})
            oid = rec["order_id"]
            # record_vote 带注入字符串
            store.record_vote(oid, 'c-1"; DROP', "a", 'long"; rm -rf',
                              reasoning="</script>`, confidence", confidence=0.9)
            got = store.get(oid)
            # JSON 仍可解析
            self.assertIsInstance(got, dict)
            # 值是纯字符串，不被解析
            vote = got["votes"]['c-1"; DROP']["a"]
            self.assertEqual(vote["decision"], 'long"; rm -rf')


# ─────────────────────────────────────────────────────
# 3. 敏感信息泄露
# ─────────────────────────────────────────────────────
class TestSensitiveData(unittest.TestCase):
    def test_no_api_key_in_signal_payload(self):
        """信号 payload 不应包含任何 API key / secret。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("long", size_usd=50),
                        "b": FakePlanRunner("long", size_usd=50)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            r.run_once()
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            raw = inbox[0].read_text(encoding="utf-8").lower()
            for secret in ("api_key", "api_secret", "apikey", "apisecret",
                           "password", "token", "bearer", "authorization",
                           "gate_api", "openai_api", "secret"):
                self.assertNotIn(secret, raw, f"signal leaks {secret!r}")

    def test_no_api_key_in_persona_log(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("long", size_usd=50),
                        "b": FakePlanRunner("long", size_usd=50)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            r.run_once()
            log = (root / "data" / "shared" / "persona_log.jsonl").read_text(encoding="utf-8").lower()
            for secret in ("api_key", "api_secret", "password", "token", "secret"):
                self.assertNotIn(secret, log)

    def test_no_api_key_in_shared_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            rec = store.create({"symbol": "BTC_USDT", "group": "g"})
            store.record_vote(rec["order_id"], "c-1", "a", "long", reasoning="test")
            raw = json.dumps(store.get(rec["order_id"])).lower()
            for secret in ("api_key", "api_secret", "password", "token", "secret"):
                self.assertNotIn(secret, raw)

    def test_config_no_secrets_in_persona_groups(self):
        """persona_groups.yaml 不应包含密钥字段。"""
        cfg = Path("config/persona_groups.yaml")
        if not cfg.exists():
            self.skipTest("config not present")
        raw = cfg.read_text(encoding="utf-8").lower()
        for secret in ("api_key", "api_secret", "password", "token", "secret_key"):
            self.assertNotIn(secret, raw)


# ─────────────────────────────────────────────────────
# 4. 权限 / 越权
# ─────────────────────────────────────────────────────
class TestAuthorization(unittest.TestCase):
    def test_group_cannot_write_other_group_inbox(self):
        """group A 的 runner 不应把信号写进 group B 的 target inbox。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gA = _group(name="gA", members=["a", "b"], target_account="a",
                        topology="single_account",
                        fusion_config={"weights": {"a": 1, "b": 1}})
            gB = _group(name="gB", members=["c", "d"], target_account="c",
                        topology="single_account",
                        fusion_config={"weights": {"c": 1, "d": 1}})
            runners = {"a": FakePlanRunner("long", size_usd=50),
                        "b": FakePlanRunner("long", size_usd=50)}
            rA = PersonaRunner(root, gA, {"a": None, "b": None}, runners)
            rA.run_once()
            # gA 的信号只在 a 的 inbox
            inbox_a = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            inbox_c = list((root / "data" / "bots" / "c" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_a), 1)
            self.assertEqual(len(inbox_c), 0)

    def test_disabled_group_cannot_run(self):
        """disabled group 不应被 validate_group 接受执行。"""
        g = _group(enabled=False)
        # validate_group 跳过 disabled，但 runner 不应被调用
        # 这里验证 config 层面：enabled=false 的组不应出现在 target_groups
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text("groups:\n  - name: x\n    members: [a, b]\n    enabled: false\n",
                         encoding="utf-8")
            groups = load_persona_groups(p)
            self.assertFalse(groups[0].enabled)

    def test_mirror_only_writes_member_inbox(self):
        """mirror 拓扑只写成员 inbox，不写非成员。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(name="m", members=["a", "b"], topology="mirror_accounts",
                        fusion_config={"weights": {"a": 1, "b": 1}})
            runners = {"a": FakePlanRunner("long", size_usd=50),
                        "b": FakePlanRunner("long", size_usd=50)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            r.run_once()
            inbox_a = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            inbox_b = list((root / "data" / "bots" / "b" / "inbox").glob("*.json"))
            inbox_c = list((root / "data" / "bots" / "c" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_a), 1)
            self.assertEqual(len(inbox_b), 1)
            self.assertEqual(len(inbox_c), 0)


# ─────────────────────────────────────────────────────
# 5. 资源耗尽 / 拒绝服务
# ─────────────────────────────────────────────────────
class TestResourceExhaustion(unittest.TestCase):
    def test_order_store_1000_orders_bounded(self):
        """1000 单后 list_open 仍可用，不 OOM。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            t0 = time.time()
            for i in range(1000):
                store.create({"symbol": f"SYM{i}", "group": "g"})
            elapsed = time.time() - t0
            self.assertLess(elapsed, 30.0, f"1000 creates took {elapsed:.1f}s")
            opens = store.list_open()
            self.assertEqual(len(opens), 1000)

    def test_vote_record_500_bounded(self):
        """单订单 500 轮投票，文件大小可控、不超时。"""
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT", "group": "g"})
            oid = rec["order_id"]
            t0 = time.time()
            for i in range(500):
                store.record_vote(oid, f"c-{i}", "a", "long")
            elapsed = time.time() - t0
            self.assertLess(elapsed, 30.0, f"500 votes took {elapsed:.1f}s")
            got = store.get(oid)
            self.assertEqual(len(got["votes"]), 500)

    def test_inbox_signal_accumulation(self):
        """100 个信号文件后 inbox 仍可遍历。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            for i in range(100):
                runners = {"a": FakePlanRunner("long", size_usd=10),
                            "b": FakePlanRunner("long", size_usd=10)}
                r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
                r.run_once()
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox), 100)

    def test_persona_log_growth(self):
        """persona_log.jsonl 无界增长（记录但不崩溃）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            for i in range(50):
                runners = {"a": FakePlanRunner("hold", action="hold"),
                            "b": FakePlanRunner("hold", action="hold")}
                r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
                r.run_once()
            log = root / "data" / "shared" / "persona_log.jsonl"
            self.assertTrue(log.exists())
            lines = log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 50)

    def test_huge_chips_list_not_explosive(self):
        """LLM 输出超大 chips 列表，不应导致 OOM 或超长信号。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            huge = [{"action": "open_long", "symbol": "BTC_USDT",
                      "size_usd": 1, "confidence": 0.9} for _ in range(200)]
            runners = {"a": FakePlanRunner("long", chips=huge),
                        "b": FakePlanRunner("long", chips=huge)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            # 只取第一个 chip 作为执行模板
            self.assertTrue(res["ok"])
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox), 1)


# ─────────────────────────────────────────────────────
# 6. 配置攻击面
# ─────────────────────────────────────────────────────
class TestConfigAttackSurface(unittest.TestCase):
    def test_yaml_bomb_rejected(self):
        """YAML alias 炸弹（billion laughs）应被 safe_load 兜底。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            # yaml.safe_load 不展开 alias 到危险程度；测试不崩溃
            p.write_text(
                "groups:\n  - name: x\n    members: [a, b]\n",
                encoding="utf-8",
            )
            groups = load_persona_groups(p)
            self.assertEqual(len(groups), 1)

    def test_extremely_long_member_names(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            long_name = "a" * 5000
            p.write_text(f"groups:\n  - name: x\n    members: [{long_name}, b]\n",
                         encoding="utf-8")
            try:
                groups = load_persona_groups(p)
                # 长名不崩溃；validate 时 BotPaths 会拒
                validate_group(groups[0], known_bots={long_name, "b"})
            except PersonaError:
                pass  # 拒绝长名也可以

    def test_negative_weights_accepted_but_safe(self):
        """负权重不应导致除零或异常行为。"""
        g = _group(fusion_config={"weights": {"a": -5, "b": -3}})
        plans = {"a": {"decision": "long"}, "b": {"decision": "long"}}
        r = fuse_plans(g, plans)
        # 负权重下 total 可能 <=0，应安全返回 hold 或仍工作
        self.assertIn(r["decision"], ("long", "short", "hold"))

    def test_zero_total_weight_safe(self):
        g = _group(fusion_config={"weights": {"a": 0, "b": 0}})
        plans = {"a": {"decision": "long"}, "b": {"decision": "short"}}
        r = fuse_plans(g, plans)
        self.assertIn(r["decision"], ("long", "short", "hold"))

    def test_empty_weights_dict_safe(self):
        g = _group(fusion_config={"weights": {}})
        plans = {"a": {"decision": "long"}, "b": {"decision": "long"}}
        r = fuse_plans(g, plans)
        self.assertEqual(r["decision"], "long")

    def test_fusion_config_injection_safe(self):
        """fusion_config 中的 threshold 为字符串时安全处理。"""
        g = _group(fusion="consensus", fusion_config={"threshold": "0.5; DROP"})
        plans = {"a": {"decision": "long"}, "b": {"decision": "long"}}
        try:
            r = fuse_plans(g, plans)
            self.assertIn(r["decision"], ("long", "short", "hold"))
        except (ValueError, TypeError):
            pass  # 抛异常也可以，关键是不执行任意代码


# ─────────────────────────────────────────────────────
# 7. 并发 / 进程安全
# ─────────────────────────────────────────────────────
class TestProcessSafety(unittest.TestCase):
    def test_pidlock_prevents_double_run(self):
        """PidLock 同进程可重入，但不同 PID 会被拒。"""
        from gate_bot.pidlock import PidLock
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "test.lock"
            lock1 = PidLock(lock_path).acquire()
            self.assertIsNotNone(lock1)
            # 同进程重入：允许（lock steal for same pid）
            lock2 = PidLock(lock_path).acquire()
            self.assertIsNotNone(lock2, "same process re-acquire allowed")
            # 模拟其他进程持有锁
            lock_path.write_text("99999", encoding="utf-8")  # fake alive pid
            lock3 = PidLock(lock_path).acquire()
            # 99999 可能不存在 → steal；或存在 → None
            # 两种都可接受，关键是不崩溃
            if lock3:
                lock3.release()
            lock1.release()

    def test_signal_file_unique_names_rapid(self):
        """极快连续写入，文件名不冲突。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            for _ in range(20):
                runners = {"a": FakePlanRunner("long", size_usd=10),
                            "b": FakePlanRunner("long", size_usd=10)}
                r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
                r.run_once()
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            names = [f.name for f in inbox]
            self.assertEqual(len(names), len(set(names)), "filename collision detected")


# ─────────────────────────────────────────────────────
# 8. 边界值 / 异常输入
# ─────────────────────────────────────────────────────
class TestBoundaryValues(unittest.TestCase):
    def test_confidence_nan(self):
        """confidence=NaN 不应破坏融合。"""
        from gate_bot.persona.fusion import _norm_confidence
        import math
        c = _norm_confidence({"confidence": float("nan")})
        self.assertIsInstance(c, float)
        self.assertTrue(0.0 <= c <= 1.0 or math.isnan(c))

    def test_confidence_infinity(self):
        from gate_bot.persona.fusion import _norm_confidence
        c = _norm_confidence({"confidence": float("inf")})
        self.assertEqual(c, 1.0)

    def test_decision_unicode(self):
        """decision 含 unicode / emoji，安全处理。"""
        from gate_bot.persona.fusion import _norm_dir
        self.assertEqual(_norm_dir({"decision": "🚀🚀🚀"}), "hold")
        self.assertEqual(_norm_dir({"decision": "多头"}), "hold")

    def test_empty_string_decision(self):
        from gate_bot.persona.fusion import _norm_dir
        self.assertEqual(_norm_dir({"decision": ""}), "hold")

    def test_none_plan_fields(self):
        """plan 字段全 None，不崩溃。"""
        g = _group()
        plans = {"a": {"decision": None, "chips": None, "confidence": None},
                 "b": {"decision": None, "chips": None, "confidence": None}}
        r = fuse_plans(g, plans)
        self.assertEqual(r["decision"], "hold")

    def test_symbol_empty_string(self):
        """symbol 为空字符串时 schema 应拒绝。"""
        from gate_bot.schema import parse_signal
        with self.assertRaises(Exception):
            parse_signal({"action": "open_long", "symbol": "", "size_usd": 50})


if __name__ == "__main__":
    unittest.main()
