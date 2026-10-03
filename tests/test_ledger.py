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
from shopee_ledger.desk import listing_gate
from shopee_ledger.profit import Decision, ProfitInput, evaluate
from shopee_ledger.store import Ledger
from shopee_ledger.veto import veto_reasons
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
    def test_apparel_blocks_weight_does_not(self):
        self.assertEqual(veto_reasons(["apparel"]), ["服装、鞋帽"])
        self.assertEqual(veto_reasons([]), [])


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
    def test_quote_stays_incomplete_until_rates_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "ledger.sqlite")
            ledger.init()
            candidate = ledger.add_candidate("MY", "杯垫", 80, 10, 2, 20, 5, True)
            ledger.set_return_rate(candidate, 0.05)
            self.assertEqual(ledger.quote(candidate).decision, Decision.INCOMPLETE)
            for key, value in (
                ("local_per_cny", "0.6"),
                ("commission_rate", "0.08"),
                ("transaction_rate", "0.02"),
                ("service_fee_kind", "service"),
                ("service_fee_rate", "0.025"),
                ("withdrawal_rate", "0.01"),
                ("fx_loss_rate", "0.01"),
            ):
                ledger.set_param("MY", key, value, "E")
            result = ledger.quote(candidate)
            self.assertEqual(result.decision, Decision.GO)
            passed, complete, rate = ledger.survival("MY")
            self.assertEqual((passed, complete, rate), (1, 1, 1.0))
            order = ledger.open_order(candidate)
            supplier = ledger.add_supplier("甲", "https://example.test", 3, True, True, 10)
            ledger.apply_stock(order, supplier, True, False)
            ledger.apply_arrange(order)
            ledger.apply_copy(order, "当单中转仓 订单号SN1")
            ledger.apply_purchase(order)
            ledger.apply_inbound(order)
            self.assertEqual(ledger.load_order(order).status, "done")
            self.assertEqual(len(ledger.checklist_rows()), 22)
            ledger.close()


class DeskTest(unittest.TestCase):
    def test_go_still_needs_sample_and_three_suppliers(self):
        result = evaluate(sample())
        self.assertEqual(result.decision, Decision.GO)
        text = listing_gate(result, 1, False, True, False, False, False)
        self.assertEqual(text, "先买样品再称重")
        ready = listing_gate(result, 3, True, True, True, True, True)
        self.assertEqual(ready, "可上架")


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
                self.assertIn("马来站存活率", home)
                body = urlencode({"site": "MY", "key": "local_per_cny", "value": "0.6", "grade": "C"}).encode()
                saved = urlopen(Request(f"http://127.0.0.1:{port}/params", data=body)).read().decode()
                self.assertIn("0.6", saved)
                self.assertIn("参数已保存", saved)
            finally:
                httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
