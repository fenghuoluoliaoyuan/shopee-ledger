"""页面契约测试。

最强的一条：**/orders 暴露的操作入口必须恰好等于状态机允许的转移**。
写死按钮的时代，spec 里加了状态而页面没入口——人就走不下去。
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.store import Ledger
from shopee_ledger.web import orders_page, products_page


class OrdersPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.init()
        self.ledger.set_param("TW", "local_per_cny", "4.5", "C")
        self.candidate = self.ledger.add_candidate("TW", "杯垫", 80, 20, 1.5, 350, 60, True)
        self.supplier = self.ledger.add_supplier("甲", "https://example.test", 3, True, True, 10)
        self.order = self.ledger.open_order(self.candidate)

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _row(self) -> dict:
        return [row for row in self.ledger.list_orders() if row["id"] == self.order][0]

    def _exposed(self) -> set:
        """页面上可点的目标状态。"""
        html = orders_page(self.ledger)
        targets = set(re.findall(r'name="to" value="([a-z_]+)"', html))
        if 'name="action" value="cancel"' in html:
            targets.add("cancelled")
        return targets

    def _advance_one(self) -> str:
        """走一步。每一步都用 advance_order（apply_inbound 一次走两态，会与逐步走查冲突）。"""
        target = self._row()["next_states"][0]
        if target == "stock_checked":
            self.ledger.apply_stock(self.order, self.supplier, True, False)
        elif target == "address_captured":
            self.ledger.apply_copy(self.order, "当单中转仓 订单号SN1")
        elif target == "ship_arranged":
            self.ledger.apply_arrange(self.order)
        elif target == "po_created":
            self.ledger.apply_purchase(self.order)
        else:
            self.ledger.advance_order(self.order, target)
        return target

    def test_ui_exposes_exactly_the_allowed_transitions(self):
        visited = []
        for _ in range(13):
            row = self._row()
            allowed = set(row["next_states"])
            exposed = self._exposed()
            self.assertEqual(exposed, allowed,
                             "状态 %s：页面显示 %s，状态机允许 %s" % (row["status"], sorted(exposed), sorted(allowed)))
            visited.append(row["status"])
            if not allowed:
                break
            self._advance_one()
        self.assertEqual(visited[0], "created")
        self.assertEqual(visited[-1], "paid", "沿合法路径要能一路走到已到账")
        self.assertEqual(len(visited), 12, "主路径状态数变了：%s" % visited)

    def test_guard_text_is_shown_next_to_the_button(self):
        self.ledger.apply_stock(self.order, self.supplier, True, False)
        html = orders_page(self.ledger)
        self.assertIn("禁止跳步", html, "守卫说明要显示出来，人要知道为什么只能走这一步")

    def test_terminal_state_has_no_buttons(self):
        for _ in range(12):
            if not self._row()["next_states"]:
                break
            self._advance_one()
        html = orders_page(self.ledger)
        self.assertIn("已到终态", html)
        self.assertEqual(self._exposed(), set())

    def _post(self, data: dict) -> str:
        from shopee_ledger.web import _order_post

        return _order_post(self.ledger, {key: [value] for key, value in data.items()})

    def test_post_handler_walks_the_whole_main_path(self):
        """走 HTTP 处理函数本身，而不是绕开它直接调 advance_order。

        这里漏过一个真 bug：从 supplier_shipped 点「到仓扫描」会调到 apply_inbound，
        而它内部先走 supplier_shipped，于是点完原地不动。
        """
        self._post({"action": "open", "candidate": str(self.candidate)})
        order = self.ledger.list_orders()[0]["id"]
        self._post({"action": "advance", "order": str(order), "to": "stock_checked",
                    "supplier": str(self.supplier), "in_stock": "yes"})
        self._post({"action": "advance", "order": str(order), "to": "ship_arranged"})
        self._post({"action": "advance", "order": str(order), "to": "address_captured",
                    "address": "当单中转仓 订单号SN1"})
        self._post({"action": "advance", "order": str(order), "to": "po_created"})
        for target in ("supplier_shipped", "warehouse_scanned", "in_transit", "delivered",
                       "completed", "payable", "paid"):
            self._post({"action": "advance", "order": str(order), "to": target})
            current = [item for item in self.ledger.list_orders() if item["id"] == order][0]["status"]
            self.assertEqual(current, target, "点「%s」之后状态应当就是 %s" % (target, target))

    def test_post_error_comes_back_as_a_message(self):
        self._post({"action": "open", "candidate": str(self.candidate)})
        order = self.ledger.list_orders()[0]["id"]
        result = self._post({"action": "advance", "order": str(order), "to": "paid"})
        self.assertIn("error=", result, "跳步应回成页面提示，而不是 500")

    def test_cancelled_order_shows_cancel_marker(self):
        self.ledger.apply_stock(self.order, self.supplier, False, True)
        html = orders_page(self.ledger)
        self.assertIn("已取消", html)
        self.assertEqual(self._exposed(), set())


class ProductsPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.init()
        self.ledger.set_param("TW", "local_per_cny", "4.5", "C")
        self.candidate = self.ledger.add_candidate("TW", "杯垫", 80, 20, 1.5, 350, 60, True)

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_each_candidate_has_an_update_entry(self):
        """录入之后还要能改：买样、称重、拍图、写标题都是后来的事。"""
        html = products_page(self.ledger)
        self.assertRegex(html, r"""name=['"]action['"] value=['"]update['"]""")
        self.assertRegex(html, r"""name=['"]id['"] value=['"]%d['"]""" % self.candidate)

    def test_page_shows_all_four_gates(self):
        html = products_page(self.ledger)
        for gate_id in ("G1", "G2", "G3", "G4"):
            self.assertIn(gate_id, html, "四道门禁的状态要逐门显示，不能只给一句结论")

    def test_update_advances_the_gate(self):
        """端到端：样品到手 + 称重之后，G2 不再卡在这一条。"""
        from shopee_ledger.web import _update_product

        self.assertIn("G2 INCOMPLETE", products_page(self.ledger), "样品没买时 G2 应当卡住")
        for index in range(3):
            self.ledger.link_supplier(
                self.candidate,
                self.ledger.add_supplier("供应商%d" % index, "https://e.test", 3, True, True, 9))
        _update_product(self.ledger, {
            "id": [str(self.candidate)], "sample": ["yes"], "measured": ["yes"],
            "weight": ["95"], "photo": ["yes"], "detail": ["yes"], "title": ["杯垫 硅藻泥 吸水"],
        })
        row = [item for item in self.ledger.list_candidates() if item["id"] == self.candidate][0]
        self.assertEqual(row["weight_g"], 95.0, "称重时顺手把数字一起更新")
        self.assertTrue(row["weight_is_measured"])
        self.assertTrue(row["sample_bought"])
        after = products_page(self.ledger)
        self.assertNotIn("G2 INCOMPLETE", after, "G2 应当已经过关")
        self.assertNotIn("重量还是估算值", after)

    def test_estimated_weight_keeps_g2_blocked(self):
        from shopee_ledger.web import _update_product

        _update_product(self.ledger, {"id": [str(self.candidate)], "sample": ["yes"], "weight": ["80"]})
        self.assertIn("重量还是估算值", products_page(self.ledger))


if __name__ == "__main__":
    unittest.main()
