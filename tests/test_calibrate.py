"""KPI 校准的契约测试。

两条最重要的断言：
1. **有数据时必须真能发现偏离**——否则这就只是个"永远报数据不足"的摆设。
2. **样本不足时绝不给建议值**——拿三个订单算出的"实际退货率"去改阈值比不改更危险。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.calibrate import (  # noqa: E402
    CONSISTENT,
    DRIFT,
    INSUFFICIENT,
    compare_rate,
    compare_threshold,
    render,
    summary,
)
from shopee_ledger.store import Ledger  # noqa: E402


def escrow(commission: float, transaction: float = 9.0):
    return {"order_income": {"actual_shipping_fee": 60, "buyer_paid_shipping_fee": 0,
                             "commission_fee": commission, "transaction_fee": transaction}}


class CompareRateTest(unittest.TestCase):
    def test_insufficient_below_min_samples(self):
        item = compare_rate("P-X", "费率", 0.14, [0.14, 0.14], min_samples=5)
        self.assertEqual(item.status, INSUFFICIENT)
        self.assertIsNone(item.observed)
        self.assertTrue(item.needs, "不足时要写清还缺什么")

    def test_consistent_within_tolerance(self):
        item = compare_rate("P-X", "费率", 0.14, [0.14] * 6, min_samples=5)
        self.assertEqual(item.status, CONSISTENT)
        self.assertAlmostEqual(item.observed, 0.14, places=6)

    def test_drift_beyond_tolerance(self):
        item = compare_rate("P-X", "费率", 0.14, [0.20] * 6, min_samples=5)
        self.assertEqual(item.status, DRIFT)
        self.assertTrue(item.actionable)
        self.assertGreater(item.deviation, 0.10)

    def test_uses_median_so_one_odd_bill_does_not_trigger_drift(self):
        """账单里偶有异常单（退款调整）。用平均值会被带偏，用中位数不会。"""
        item = compare_rate("P-X", "费率", 0.14, [0.14] * 9 + [5.0], min_samples=5)
        self.assertEqual(item.status, CONSISTENT)
        self.assertAlmostEqual(item.observed, 0.14, places=6)

    def test_none_values_are_dropped_not_treated_as_zero(self):
        item = compare_rate("P-X", "费率", 0.14, [0.14, None, 0.14, None] * 3, min_samples=5)
        self.assertEqual(item.samples, 6)
        self.assertEqual(item.status, CONSISTENT)

    def test_tolerance_is_relative_not_absolute(self):
        """费率量级小，容差必须是相对的：0.14 上偏 0.005 只差 3.6%，不算漂移。"""
        item = compare_rate("P-X", "费率", 0.14, [0.145] * 6, min_samples=5, tolerance=0.10)
        self.assertEqual(item.status, CONSISTENT)


class CompareThresholdTest(unittest.TestCase):
    def test_insufficient_below_min_samples(self):
        item = compare_threshold("P-T", "阈值", {"pass": 0.15}, [0.1, 0.2], min_samples=5)
        self.assertEqual(item.status, INSUFFICIENT)
        self.assertIn("样本", item.needs)

    def test_reports_which_side_the_observation_falls_on(self):
        item = compare_threshold("P-T", "阈值", {"pass": 0.15}, [0.04] * 6, min_samples=5)
        self.assertIn("低于", item.note)
        self.assertIn("偏严", item.note)

    def test_reports_when_the_threshold_is_easy(self):
        item = compare_threshold("P-T", "阈值", {"pass": 0.15}, [0.40] * 6, min_samples=5)
        self.assertIn("高于", item.note)

    def test_threshold_without_a_comparable_scalar_still_reports_observed(self):
        item = compare_threshold("P-T", "阈值", {"min": 0.03, "max": 0.05}, [0.04] * 6,
                                 min_samples=5)
        self.assertIsNotNone(item.observed)
        self.assertIn("实测中位数", item.note)


class SummaryTest(unittest.TestCase):
    def test_counts_by_status_and_sums_samples(self):
        items = [compare_rate("A", "a", 0.1, [0.1] * 5, min_samples=5),
                 compare_rate("B", "b", 0.1, [0.5] * 5, min_samples=5),
                 compare_rate("C", "c", 0.1, [], min_samples=5)]
        stats = summary(items)
        self.assertEqual(stats["consistent"], ["A"])
        self.assertEqual(stats["drift"], ["B"])
        self.assertEqual(stats["insufficient"], ["C"])
        self.assertEqual(stats["samples"], 10)

    def test_render_states_the_no_auto_change_rule(self):
        items = [compare_rate("A", "a", 0.1, [], min_samples=5)]
        text = render(items)
        self.assertIn("只报告，不自动改阈值", text)
        self.assertIn("数据不足", text)


class LedgerCalibrationTest(unittest.TestCase):
    """接上数据库：真实存储的通路能不能发现偏离。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite", verified_path=None)
        self.ledger.init()
        self.ledger.set_param("TW", "local_per_cny", "4.5", "C")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def _make_orders(self, count: int, commission: float, price: float = 350.0):
        for index in range(count):
            candidate = self.ledger.add_candidate("TW", "杯垫%d" % index, 80, 20, 1.5,
                                                  price, 60, True)
            order = self.ledger.open_order(candidate)
            self.ledger.record_actual(order, escrow(commission))

    def _item(self, items, param_id):
        return next(item for item in items if item.param_id == param_id)

    def test_empty_database_reports_insufficient_with_what_is_needed(self):
        items = self.ledger.calibration_items()
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item.status, INSUFFICIENT)
            self.assertTrue(item.needs)

    def test_detects_a_commission_drift_from_real_bill_amounts(self):
        """账单里的佣金反算出 20%，而参数写的是 14% → 必须报偏离。

        这条是整个校准器的意义所在：光有"数据不足"的报告没有价值。
        """
        self._make_orders(6, commission=70.0)     # 70/350 = 20%
        item = self._item(self.ledger.calibration_items(), "P-TW-COMMISSION")
        self.assertEqual(item.status, DRIFT, item.note)
        self.assertAlmostEqual(item.observed, 0.20, places=4)
        self.assertGreater(item.samples, 4)

    def test_matching_bill_amounts_report_consistent(self):
        self._make_orders(6, commission=49.0)     # 49/350 = 14%，与参数一致
        item = self._item(self.ledger.calibration_items(), "P-TW-COMMISSION")
        self.assertEqual(item.status, CONSISTENT, item.note)

    def test_realized_margin_sample_is_collected(self):
        """净利率阈值的样本来自 Order.actual_rate。"""
        self._make_orders(6, commission=49.0)
        item = self._item(self.ledger.calibration_items(), "P-TW-MARGIN-TH")
        self.assertGreaterEqual(item.samples, 5)
        self.assertIsNotNone(item.observed)


if __name__ == "__main__":
    unittest.main()
