"""落地成本对比的契约测试。

最关键的一条是**回代验证**：把算出的「最低售价」喂回 CostEngine，
净利率必须等于目标值。解析解写错了的话，这个测试会立刻发现——
比盯着公式看可靠得多。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.cost_engine import CostEngine, CostInputs  # noqa: E402
from shopee_ledger.landed import LandedRow, compare, render  # noqa: E402
from shopee_ledger.spec import Spec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def engine() -> CostEngine:
    return CostEngine(Spec.load(ROOT / "spec"))


class AnalyticSolutionTest(unittest.TestCase):
    """解析解必须自洽：代回去要正好落在目标净利率上。

    注意：回代时**必须把同一组费率显式传给 CostInputs**。不传的话引擎会去参数里取
    提现/汇损/退货率（P-TW-WITHDRAW 等），而 compare() 用的是默认 0——两边口径不同，
    测试会假失败（第一版就是这么写错的）。
    """

    def setUp(self):
        self.spec = Spec.load(ROOT / "spec")

    def _quote(self, row, **rates):
        return engine().quote(CostInputs(
            market=row.market, price_local=row.min_price, purchase_cny=20.0,
            domestic_cny=2.0, local_per_cny=row.local_per_cny, sls_freight=row.seller_freight,
            buyer_paid_freight=row.buyer_pays_freight, seller_pays_freight=False,
            **rates))

    def test_min_price_reproduces_the_target_margin(self):
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=2.0, weight_g=500.0,
                       fx_by_market={"TW": 4.49}, target_margin=0.15, markets=["TW"],
                       channels_per_market=1, channel_filter="7-11")
        row = next(item for item in rows if item.feasible)
        result = self._quote(row, withdraw_rate=0.0, fx_loss_rate=0.0, return_rate=0.0)
        self.assertTrue(result.computed, result.missing)
        self.assertAlmostEqual(result.rate, 0.15, places=4,
                               msg="回代后的净利率应当等于目标净利率")

    def test_min_price_holds_for_another_weight_and_margin(self):
        rows = compare(self.spec, purchase_cny=35.0, domestic_cny=3.0, weight_g=1200.0,
                       fx_by_market={"TW": 4.49}, target_margin=0.10, markets=["TW"],
                       channels_per_market=1, channel_filter="蝦皮店到店")
        row = next(item for item in rows if item.feasible)
        result = engine().quote(CostInputs(
            market="TW", price_local=row.min_price, purchase_cny=35.0, domestic_cny=3.0,
            local_per_cny=4.49, sls_freight=row.seller_freight,
            buyer_paid_freight=row.buyer_pays_freight, seller_pays_freight=False,
            withdraw_rate=0.0, fx_loss_rate=0.0, return_rate=0.0))
        self.assertAlmostEqual(result.rate, 0.10, places=4)

    def test_with_proportional_extras_it_still_balances(self):
        """提现与汇损也是按售价计提的，解析解要把它们算进 k。"""
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=2.0, weight_g=500.0,
                       fx_by_market={"TW": 4.49}, target_margin=0.12, markets=["TW"],
                       channels_per_market=1, channel_filter="7-11",
                       withdraw_rate=0.012, fx_loss_rate=0.005, return_rate=0.05)
        row = next(item for item in rows if item.feasible)
        result = engine().quote(CostInputs(
            market="TW", price_local=row.min_price, purchase_cny=20.0, domestic_cny=2.0,
            local_per_cny=4.49, sls_freight=row.seller_freight,
            buyer_paid_freight=row.buyer_pays_freight, seller_pays_freight=False,
            withdraw_rate=0.012, fx_loss_rate=0.005, return_rate=0.05))
        self.assertAlmostEqual(result.rate, 0.12, places=4)


class InfeasibleTest(unittest.TestCase):
    def setUp(self):
        self.spec = Spec.load(ROOT / "spec")

    def test_impossible_target_reports_infeasible_not_a_number(self):
        """费率合计 + 目标净利率 ≥ 100% 时无解——必须报「做不了」，不能给负数或无穷大。"""
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=0.0, weight_g=500.0,
                       fx_by_market={"TH": 5.07}, target_margin=0.80, markets=["TH"],
                       channels_per_market=1)
        for row in rows:
            self.assertFalse(row.feasible)
            self.assertIsNone(row.min_price)
            self.assertTrue(row.notes, "无解要说明原因")

    def test_missing_rates_are_listed_not_substituted_by_zero(self):
        """BR 没有佣金参数——不能按 0 算出一个漂亮的价格。"""
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=0.0, weight_g=500.0,
                       fx_by_market={"BR": 0.78}, target_margin=0.15, markets=["BR"],
                       channels_per_market=1)
        self.assertTrue(rows)
        for row in rows:
            self.assertFalse(row.feasible)
            self.assertIn("commission", row.missing)

    def test_missing_fx_is_reported(self):
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=0.0, weight_g=500.0,
                       fx_by_market={}, target_margin=0.15, markets=["TW"],
                       channels_per_market=1)
        self.assertTrue(rows)
        self.assertIn("local_per_cny", rows[0].missing)


class CrossCurrencyTest(unittest.TestCase):
    def setUp(self):
        self.spec = Spec.load(ROOT / "spec")

    def test_cny_column_converts_by_the_rate(self):
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=2.0, weight_g=500.0,
                       fx_by_market={"TW": 4.49}, target_margin=0.15, markets=["TW"],
                       channels_per_market=1, channel_filter="7-11")
        row = next(item for item in rows if item.feasible)
        self.assertAlmostEqual(row.min_price_cny, row.min_price / 4.49, places=6)

    def test_rows_are_sorted_by_cny_comparable_price(self):
        """排序按本币会被汇率误导（VND 数字大得多），所以必须按可比的量排。"""
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=2.0, weight_g=500.0,
                       fx_by_market={"TW": 4.49, "MY": 0.65, "TH": 5.07},
                       target_margin=0.15, channels_per_market=1)
        cny = [row.min_price_cny for row in rows if row.feasible]
        self.assertEqual(cny, sorted(cny), "可行行应当按折人民币价格升序")


class ChannelSelectionTest(unittest.TestCase):
    def setUp(self):
        self.spec = Spec.load(ROOT / "spec")

    def test_cheapest_channels_come_first(self):
        """按运费从低到高挑渠道——按名字长短会挑出跟跨境直邮无关的渠道。"""
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=0.0, weight_g=500.0,
                       fx_by_market={"TW": 4.49}, target_margin=0.15, markets=["TW"],
                       channels_per_market=3)
        freights = [row.seller_freight for row in rows if row.feasible]
        self.assertEqual(freights, sorted(freights))

    def test_channel_filter_narrows_the_list(self):
        rows = compare(self.spec, purchase_cny=20.0, domestic_cny=0.0, weight_g=500.0,
                       fx_by_market={"TW": 4.49}, target_margin=0.15, markets=["TW"],
                       channels_per_market=5, channel_filter="7-11")
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("7-11", row.channel)

    def test_special_cargo_is_priced_higher(self):
        normal = compare(self.spec, purchase_cny=20.0, domestic_cny=0.0, weight_g=1200.0,
                         fx_by_market={"TW": 4.49}, target_margin=0.15, markets=["TW"],
                         channels_per_market=1, channel_filter="蝦皮店到店", cargo="Normal")
        special = compare(self.spec, purchase_cny=20.0, domestic_cny=0.0, weight_g=1200.0,
                          fx_by_market={"TW": 4.49}, target_margin=0.15, markets=["TW"],
                          channels_per_market=1, channel_filter="蝦皮店到店", cargo="Special")
        self.assertGreater(special[0].min_price, normal[0].min_price)


class RenderTest(unittest.TestCase):
    def test_render_mentions_the_cross_currency_caveat(self):
        rows = [LandedRow(market="TW", cargo="Normal", channel="ch", currency="TWD",
                          seller_freight=25.0, buyer_pays_freight=45.0, commission=0.14,
                          txn_fee=0.025, tech_fee=0.0, infra_fee=0.0, local_per_cny=4.49,
                          fixed_cost=100.0, min_price=144.2, landed_cost=122.5)]
        text = render(rows, target_margin=0.15, purchase_cny=20.0, domestic_cny=2.0,
                      weight_g=500.0)
        self.assertIn("折人民币", text)
        self.assertIn("没有可比性", text)

    def test_render_shows_the_reason_for_infeasible_rows(self):
        row = LandedRow(market="BR", cargo="Normal", channel="ch", currency="BRL",
                        seller_freight=10.0, buyer_pays_freight=0.0, commission=0.0,
                        txn_fee=0.0, tech_fee=0.0, infra_fee=0.0, feasible=False,
                        missing=["commission"])
        text = render([row], target_margin=0.15, purchase_cny=20.0, domestic_cny=0.0,
                      weight_g=500.0)
        self.assertIn("做不了", text)
        self.assertIn("commission", text)


if __name__ == "__main__":
    unittest.main()
