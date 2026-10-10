# -*- coding: utf-8 -*-
"""`list_orders` / `list_price_orders` 的**翻页**行为。

回归背景（审计 C-14 的延伸）：Gate 一页默认 100 条，超出就静默截断，而调用方
（executor 的归属扫描、watcher 的守护扫描）把这批单当作「本 bot 在该合约上的
全部挂单」用 —— 截断 = 漏判归属 = 对账/清理看不见真实存在的保护单（fail-open）。

三个不变量：
  1. 服务端正常翻页 → 全取到（不是只取第一页）
  2. 服务端忽略 `offset` → **提前结束**，绝不把同一页重复 N 倍（宁可少取也不能重复）
  3. 小账户（单页不足 limit）→ 一次请求、结果与改动前一致
"""
import re
import unittest

from omnialpha.gate_client import GateClient


class _Fake(GateClient):
    def __init__(self, total, ignore_offset=False):
        self.calls = []
        self.total = total
        self.ignore = ignore_offset

    def rest_signed_request(self, method, path, qs="", **kw):
        self.calls.append((path.rsplit("/", 1)[-1], qs))
        off = 0 if self.ignore else int(re.search(r"offset=(\d+)", qs).group(1))
        limit = int(re.search(r"limit=(\d+)", qs).group(1))
        n = max(0, min(limit, self.total - off))
        return [{"id": f"o{i}"} for i in range(off, off + n)]

    def offsets(self):
        return [re.search(r"offset=(\d+)", q).group(1) for _, q in self.calls]


class TestOpenOrderPaging(unittest.TestCase):
    def test_all_pages_collected(self):
        c = _Fake(250)
        rows = c.list_orders()
        self.assertEqual(len(rows), 250)
        self.assertEqual(len({r["id"] for r in rows}), 250, "翻页不该产生重复条目")
        self.assertEqual(c.offsets(), ["0", "100", "200"])

    def test_no_contract_filter_omits_param(self):
        c = _Fake(10)
        c.list_orders()
        self.assertNotIn("contract=", c.calls[0][1])

    def test_contract_filter_is_kept(self):
        c = _Fake(37)
        rows = c.list_orders("BTC_USDT")
        self.assertEqual(len(rows), 37)
        self.assertEqual(len(c.calls), 1, "单页取完就停，不该多打一次")
        self.assertIn("contract=BTC_USDT", c.calls[0][1])

    def test_offset_ignored_by_server_does_not_amplify(self):
        """服务端不支持 offset 时宁可少取 —— 重复条目会让「撤销多余保护单」误判。"""
        c = _Fake(999, ignore_offset=True)
        rows = c.list_orders()
        self.assertEqual(len(rows), 100)
        self.assertEqual(len(c.calls), 2, "发现无新数据就该立刻停")

    def test_page_cap_bounds_the_scan(self):
        c = _Fake(1500)
        rows = c.list_orders()
        self.assertEqual(len(c.calls), GateClient._OPEN_MAX_PAGES)
        self.assertEqual(len(rows), GateClient._OPEN_PAGE_SIZE * GateClient._OPEN_MAX_PAGES)

    def test_price_orders_uses_same_paging(self):
        c = _Fake(150)
        rows = c.list_price_orders()
        self.assertEqual(len(rows), 150)
        self.assertEqual(c.calls[0][0], "price_orders")
        self.assertTrue(all(p == "price_orders" for p, _ in c.calls))

    def test_empty_account_is_one_request(self):
        c = _Fake(0)
        self.assertEqual(c.list_orders(), [])
        self.assertEqual(len(c.calls), 1, "空账户不该继续翻页")


if __name__ == "__main__":
    unittest.main()
