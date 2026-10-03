import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.api import sign
from shopee_ledger.escrow import map_escrow
from shopee_ledger.fulfillment import Fulfillment, arrange_ship, copy_address, mark_stock
from shopee_ledger.desk import listing_gate, listing_warnings
from shopee_ledger.profit import Decision, ProfitInput, evaluate
from shopee_ledger.store import Ledger
from shopee_ledger.veto import veto_advisories, veto_reasons
from shopee_ledger.web import Handler


def sample(**overrides) -> ProfitInput:
    data = dict(
        site="MY",
        price=20.0,
        purchase_cny=10.0,
        domestic_cny=2.0,
        local_per_cny=0.6,
        sls_fee=5.0,
        buyer_shipping=None,
        intends_free_shipping=True,
        prelist=True,
        commission=1.6,
        transaction_fee=0.4,
        service_fee=0.5,
        service_fee_kind="service",
        affiliate=0.0,
        ads=0.0,
        withdrawal=0.2,
        fx_loss=0.2,
        return_rate=0.05,
        go_rate=0.15,
        watch_rate=0.10,
    )
    data.update(overrides)
    return ProfitInput(**data)


class ProfitTest(unittest.TestCase):
    def test_free_shipping_deducts_full_sls_and_both_fees(self):
        result = evaluate(sample())
        self.assertEqual(result.net_shipping, 5.0)
        self.assertEqual(result.platform_fee, 2.5)
        self.assertAlmostEqual(result.return_reserve, 0.05 * (6.0 + 1.2))
        self.assertEqual(result.decision, Decision.GO)

    def test_buyer_paid_shipping_only_excess(self):
        result = evaluate(sample(prelist=False, buyer_shipping=3.0, intends_free_shipping=False))
        self.assertEqual(result.net_shipping, 2.0)

    def test_buyer_overpay_does_not_add_revenue(self):
        result = evaluate(sample(prelist=False, buyer_shipping=8.0, intends_free_shipping=False))
        self.assertEqual(result.net_shipping, 0.0)

    def test_shipping_kind_is_not_charged_twice(self):
        once = evaluate(sample(service_fee_kind="shipping", service_fee=5.0))
        self.assertEqual(once.platform_fee, 2.0)

    def test_missing_fee_is_not_zero(self):
        result = evaluate(sample(commission=None))
        self.assertEqual(result.decision, Decision.INCOMPLETE)
        self.assertIn("commission", result.missing)

    def test_tw_without_threshold(self):
        result = evaluate(sample(site="TW", go_rate=None, watch_rate=None))
        self.assertEqual(result.decision, Decision.THRESHOLD_UNSET)

    def test_cut_below_ten_percent(self):
        result = evaluate(sample(price=10.0))
        self.assertEqual(result.decision, Decision.CUT)


class VetoTest(unittest.TestCase):
    def test_apparel_veto_comes_from_spec(self):
        """否决判定已从写死的 VETO_FLAGS 迁到 spec/rules/platform.json (R-SEL-002)。"""
        reasons = veto_reasons(["apparel"])
        self.assertTrue(reasons)
        self.assertIn("服装", reasons[0])
        self.assertEqual(veto_reasons([]), [])

    def test_restricted_material_is_advisory_not_hard_veto(self):
        """液体/粉末/电池在 v1.4 是硬否决；词库为 E 级后只能软提示（INV-001/011）。"""
        self.assertEqual(veto_reasons(["liquid_powder_battery"]), [])
        self.assertTrue(veto_advisories(["liquid_powder_battery"]))


class FulfillmentTest(unittest.TestCase):
    def test_cannot_arrange_before_stock(self):
        state = Fulfillment()
        with self.assertRaises(ValueError):
            arrange_ship(state)

    def test_known_supplier_skips_address_question(self):
        state = Fulfillment()
        mark_stock(state, 3, True, True, False)
        arrange_ship(state)
        self.assertTrue(state.steps["ship_arranged"])

    def test_all_out_of_stock(self):
        state = Fulfillment()
        mark_stock(state, 1, False, True, True)
        with self.assertRaises(ValueError):
            arrange_ship(state)
        self.assertEqual(state.status, "all_oos")

    def test_generic_address_rejected(self):
        state = Fulfillment()
        mark_stock(state, 3, True, True, False)
        arrange_ship(state)
        with self.assertRaises(ValueError):
            copy_address(state, "某转运仓", "web")


class EscrowTest(unittest.TestCase):
    def test_maps_official_fields_and_leaves_service_unclassified(self):
        mapped = map_escrow(
            {
                "order_income": {
                    "actual_shipping_fee": 6,
                    "buyer_paid_shipping_fee": 2,
                    "commission_fee": 1.5,
                    "transaction_fee": 0.4,
                    "service_fee": 0.5,
                }
            }
        )
        self.assertEqual(mapped["net_shipping"], 4)
        self.assertIsNone(mapped["service_fee_kind"])

    def test_sign_matches_partner_base_string(self):
        digest = sign(1, "key", "/api/v2/payment/get_escrow_detail", 10, "token", 9)
        self.assertEqual(len(digest), 64)


class StoreTest(unittest.TestCase):
    def test_my_quote_incomplete_when_spec_has_no_rates(self):
        """MY 在 spec 里没有佣金/手续费参数 → 必须 INCOMPLETE，绝不把缺失费率当 0。"""
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "ledger.sqlite", verified_path=None)
            ledger.init()
            candidate = ledger.add_candidate("MY", "杯垫", 80, 10, 2, 20, 5, True)
            ledger.set_return_rate(candidate, 0.05)
            view = ledger.quote(candidate)
            self.assertEqual(view.decision, Decision.INCOMPLETE)
            self.assertIn("commission_rate", view.missing)
            ledger.close()

    def test_tw_quote_go_with_measured_overrides(self):
        """台湾站制度性费率来自 spec，本机只覆盖实测值（汇率 C 级）。"""
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "ledger.sqlite", verified_path=None)
            ledger.init()
            ledger.set_param("TW", "local_per_cny", "4.5", "C")
            candidate = ledger.add_candidate("TW", "杯垫", 80, 20, 1.5, 350, 60, True)
            ledger.set_return_rate(candidate, 0.05)
            view = ledger.quote(candidate)
            self.assertEqual(view.cost.status, "COMPUTED")
            self.assertEqual(view.decision, Decision.GO)
            self.assertAlmostEqual(view.cost.components["commission"], 350 * 0.14, places=6)

            passed, complete, rate = ledger.survival("TW")
            self.assertEqual((passed, complete, rate), (1, 1, 1.0))

            order = ledger.open_order(candidate)
            supplier = ledger.add_supplier("甲", "https://example.test", 3, True, True, 10)
            ledger.apply_stock(order, supplier, True, False)
            ledger.apply_arrange(order)
            ledger.apply_copy(order, "当单中转仓 订单号SN1")
            ledger.apply_purchase(order)
            ledger.apply_inbound(order)
            self.assertEqual(ledger.load_order(order).state, "warehouse_scanned")

            # 状态机把关顺序：不能跳过跨境运输直接到账
            with self.assertRaises(ValueError):
                ledger.advance_order(order, "paid")
            self.assertEqual(ledger.advance_order(order, "in_transit").state, "in_transit")

            # 每次转移都要留痕（跳步尝试本身就是审计线索）
            self.assertIn("order.transition",
                          [row["action"] for row in ledger.storage.list("AuditLog", limit=100)])

            self.assertEqual(len(ledger.checklist_rows()), 48, "核实任务来自 spec（48 条）")
            ledger.close()

    def test_override_layer_rejects_unverified_grade(self):
        """覆盖层只接受 A/B/C；D/E 必须走核实任务升级路径，不能直接写进覆盖值。"""
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "ledger.sqlite", verified_path=None)
            ledger.init()
            with self.assertRaises(ValueError):
                ledger.set_param("TW", "local_per_cny", "4.5", "E")
            ledger.close()

    def test_override_is_append_only(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "ledger.sqlite", verified_path=None)
            ledger.init()
            ledger.set_param("TW", "local_per_cny", "4.5", "C")
            second = Ledger(Path(folder) / "ledger.sqlite", verified_path=None)
            second.storage.connect()
            with self.assertRaises(sqlite3.IntegrityError):
                second.storage.connect().execute(
                    "UPDATE param_override SET value = 9.9 WHERE param_id = 'P-TW-FX'")
            second.close()
            ledger.close()


class DeskTest(unittest.TestCase):
    def test_sample_blocks_before_listing(self):
        text = listing_gate(
            None, supplier_count=3, sample_bought=False, weighed=True,
            measured=True, purchase_price_cny=20.0, photo_ready=True,
        )
        self.assertTrue(text.startswith("先买样品再称重"), text)

    def test_estimated_weight_is_not_enough(self):
        """重量是估的不算过 G2——上架前随手填的数字是 D 级，称过才是 C 级。"""
        text = listing_gate(
            None, supplier_count=3, sample_bought=True, weighed=True,
            measured=False, purchase_price_cny=20.0, photo_ready=True,
        )
        self.assertIn("估算值", text)

    def test_missing_purchase_price_blocks(self):
        text = listing_gate(
            None, supplier_count=3, sample_bought=True, weighed=True, measured=True,
            photo_ready=True,
        )
        self.assertIn("采购", text)

    def test_supplier_shortage_is_a_warning_not_a_block(self):
        """供应商 <3 在 v4.0 是软门禁（R-SUP-001 WARN），不再硬拦上架。"""
        text = listing_gate(
            None, supplier_count=1, sample_bought=True, weighed=True, measured=True,
            purchase_price_cny=20.0, photo_ready=True,
        )
        self.assertEqual(text, "可上架")
        notes = " ".join(listing_warnings(
            None, supplier_count=1, sample_bought=True, weighed=True, measured=True,
            purchase_price_cny=20.0, photo_ready=True,
        ))
        self.assertIn("供应商", notes)

    def test_ready_when_everything_done(self):
        text = listing_gate(
            None, supplier_count=3, sample_bought=True, weighed=True, measured=True,
            purchase_price_cny=20.0, photo_ready=True, title_ready=True, detail_ready=True,
        )
        self.assertEqual(text, "可上架")


class WebTest(unittest.TestCase):
    def test_pages_and_param_save(self):
        with tempfile.TemporaryDirectory() as folder:
            Handler.db_path = str(Path(folder) / "ledger.sqlite")
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_address[1]
            try:
                home = urlopen(f"http://127.0.0.1:{port}/").read().decode()
                self.assertIn("站存活率", home, "今日页应渲染各市场存活率卡片（市场来自 registry）")
                self.assertIn("台湾站存活率", home)
                self.assertIn("配置", home)
                body = urlencode({"site": "TW", "key": "local_per_cny", "value": "4.5", "grade": "C"}).encode()
                saved = urlopen(Request(f"http://127.0.0.1:{port}/params", data=body)).read().decode()
                self.assertIn("4.5", saved)
                self.assertIn("参数已保存", saved)
            finally:
                httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
