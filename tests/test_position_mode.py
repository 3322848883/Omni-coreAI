# -*- coding: utf-8 -*-
"""账户级持仓模式切换：`GateClient.set_position_mode`（2026-10-16）。

**背景**：GRID 账户原为 `dual`（双向），而人格 `prompts/ladder_nogate.md:1-3`
明确写着「**本版本只做多**，只挂 多腿」、§五「单向只有一条腿」—— 切 `single`
是让人格与实际一致（又一次「人格说了没人做」）。

**⚠️ 参数形态（踩坑实测 2026-10-16）**：必须走 **query string**，放 JSON body 会失败：

| 调用 | 结果 |
|---|---|
| `POST /dual_mode` body `{"dual_mode": false}` | `MISSING_REQUIRED_PARAM` |
| `POST /set_position_mode` body `{"position_mode":"single"}` | `INVALID_PARAM_VALUE` |
| `POST /dual_mode?dual_mode=false` | ✅ 成功切到 single |

**关键约束**：Gate 要求「该保证金币种下**所有**永续合约无持仓、无挂单」时才能切
（比 `set_margin_mode` 的单合约限制更严格）；有挂单时报 400 `ORDER_PENDING`。

**⚠️ 切 single 后必须重开 `no_flip`**：单向模式下反向单**不是开反向仓，而是自动
平掉现有仓** → `no_flip: false` 会让 AI 一句 `open_short` 直接平掉多头。
配置侧由部署脚本校验（`config/bots/martin-grid.yaml` 在主仓库、worktree 读不到）。
"""
import unittest

from omnialpha.gate_client import GateApiError, GateClient


def _mk(acct_mode="dual"):
    """桩客户端：按 path 分派，记录全部请求。acct_mode 可变（测缓存失效）。"""
    cli = GateClient(api_key="k", api_secret="s", env="live")
    calls = []
    box = {"mode": acct_mode}

    def fake(method, path, qs="", body=None):
        calls.append({"method": method, "path": path, "qs": qs, "body": body})
        if path.endswith("/accounts"):
            m = box["mode"]
            return {"position_mode": m, "in_dual_mode": m == "dual"}
        return {"position_mode": box["mode"]}

    cli.rest_signed_request = fake
    return cli, calls, box


class TestSetPositionMode(unittest.TestCase):
    def test_single_uses_dual_mode_query(self):
        """single → 经典 `dual_mode` 端点，且**参数在 query**、body 为空。"""
        cli, calls, _ = _mk()
        cli.set_position_mode("single")
        last = calls[-1]
        self.assertEqual("POST", last["method"])
        self.assertTrue(last["path"].endswith("/futures/usdt/dual_mode"), last["path"])
        self.assertEqual("dual_mode=false", last["qs"])
        self.assertIsNone(last["body"], "参数放 body 会报 MISSING_REQUIRED_PARAM")

    def test_dual_uses_dual_mode_true(self):
        cli, calls, _ = _mk()
        cli.set_position_mode("dual")
        self.assertTrue(calls[-1]["path"].endswith("/dual_mode"))
        self.assertEqual("dual_mode=true", calls[-1]["qs"])

    def test_dual_plus_uses_set_position_mode(self):
        """分仓（dual_plus）走 2026-04-30 新增端点（经典端点不支持三态）。"""
        cli, calls, _ = _mk()
        cli.set_position_mode("dual_plus")
        self.assertTrue(calls[-1]["path"].endswith("/set_position_mode"))
        self.assertEqual("position_mode=dual_plus", calls[-1]["qs"])

    def test_case_and_space_tolerant(self):
        cli, calls, _ = _mk()
        cli.set_position_mode(" SINGLE ")
        self.assertEqual("dual_mode=false", calls[-1]["qs"])

    def test_invalid_value_raises_without_request(self):
        cli, calls, _ = _mk()
        for bad in ("", None, "bogus", "dual_long"):
            with self.assertRaises(GateApiError):
                cli.set_position_mode(bad)
        self.assertEqual([], calls)

    def test_cache_invalidated_after_switch(self):
        """⚠️ `get_position_mode` 有 30s 缓存 —— 切换后必须失效。

        不清缓存会让引擎在切换后的半分钟里仍按旧模式走分支（`_close` 的 side
        校验、触发单的 `auto_size` 都读它）。
        """
        cli, calls, box = _mk("dual")
        self.assertEqual("dual", cli.get_position_mode())
        self.assertTrue(cli.is_dual_position_mode())
        box["mode"] = "single"          # 交易所侧已切（模拟）
        cli.set_position_mode("single")
        self.assertEqual("single", cli.get_position_mode(), "切换后仍返回缓存值")
        self.assertFalse(cli.is_dual_position_mode())

    def test_get_position_mode_defaults_single(self):
        """账户返回缺 position_mode → 默认 single（Gate 默认单向）。"""
        cli = GateClient(api_key="k", api_secret="s", env="live")
        cli.rest_signed_request = lambda m, p, q="", b=None: {}
        self.assertEqual("single", cli.get_position_mode())
        self.assertFalse(cli.is_dual_position_mode())


if __name__ == "__main__":
    unittest.main()
