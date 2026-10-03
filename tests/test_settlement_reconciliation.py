"""用官方算例对账结算口径。

为什么这是最强的验证方式
------------------------
不是自己写一套自洽的测试去证明自己自洽，而是拿**平台账单上的真实数字**回代。
算例存在 spec/reference/order-settlement-structure.json（来自 #25770 附录2，
马来西亚站点 2026-09-22）。

对账结果（逐项）
----------------
净运费 30.51、佣金 106.49、交易手续费 21.92、服务费 6.26、订单调整 121.80
——**全部吻合**。唯一差别是「平台基础设施费」0.54：
引擎加进去了（那就是 P-INFRA-FEE 里马来西亚的 0.54/单），而官方算例的
「各项费用」只列了 佣金 + 服务费 + 交易手续费。

差在哪，我拿不出证据判定；但**差多少是确定的**，所以这里把关系钉死：
    order_income(引擎) == order_income(官方) − 平台基础设施费
以后谁要是改了别的项，这条会立刻失败。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.cost_engine import COMPUTED, INCOMPLETE, CostEngine, CostInputs  # noqa: E402
from shopee_ledger.spec import Spec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 官方算例（#25770 附录2，马来西亚，跨境免退服务）
OFFICIAL = {
    "goods": 580.00,
    "freight": 30.51,
    "commission": 106.49,
    "service_fee": 6.26,
    "txn_fee": 21.92,
    "order_income": 414.82,
    "order_adjustment": 121.80,
    "final": 293.02,
    "infra_fee_my": 0.54,          # P-INFRA-FEE 里马来西亚的值
}


def quote(**overrides):
    """按官方算例的输入跑一遍引擎。卖家自身成本置零，好和「最终金额」直接比。"""
    base = dict(
        market="MY", price_local=OFFICIAL["goods"],
        purchase_cny=0.0, domestic_cny=0.0, local_per_cny=1.0,
        seller_pays_freight=True, sls_freight=OFFICIAL["freight"], buyer_paid_freight=0.0,
        actual_commission=OFFICIAL["commission"], actual_txn_fee=OFFICIAL["txn_fee"],
        service_fee=OFFICIAL["service_fee"], service_fee_kind="service",
        ad_spend=0.0, withdraw_rate=0.0, fx_loss_rate=0.0, return_rate=0.0,
        order_adjustment=OFFICIAL["order_adjustment"],
        # 官方把税记在"订单调整"里，这里不再按税率另算一遍，否则同一笔税扣两次
        duty_rate=0.0, vat_rate=0.0,
    )
    base.update(overrides)
    return CostEngine(Spec.load(ROOT / "spec")).quote(CostInputs(**base))


class OfficialExampleTest(unittest.TestCase):
    """逐项对账。"""

    def setUp(self):
        self.result = quote()
        self.components = self.result.components

    def test_it_computes(self):
        self.assertEqual(self.result.status, COMPUTED, self.result.missing)

    def test_net_freight_matches(self):
        self.assertAlmostEqual(self.components["net_freight"], OFFICIAL["freight"], places=2)

    def test_commission_matches(self):
        self.assertAlmostEqual(self.components["commission"], OFFICIAL["commission"], places=2)

    def test_transaction_fee_matches(self):
        self.assertAlmostEqual(self.components["txn_fee"], OFFICIAL["txn_fee"], places=2)

    def test_order_adjustment_matches(self):
        self.assertAlmostEqual(self.components["order_adjustment"],
                               OFFICIAL["order_adjustment"], places=2)

    def test_the_official_formula_holds_within_the_engine(self):
        """订单收入 = 商品总额 − 运费总额 − 优惠券与回扣 − 各项费用（引擎自己必须自洽）。"""
        c = self.components
        expected = (OFFICIAL["goods"] - c["net_freight"] - c["platform_fee"]
                    - c["affiliate"] - c["coupon_discount"])
        self.assertAlmostEqual(c["order_income"], expected, places=6)

    def test_final_amount_is_order_income_minus_adjustment(self):
        """最终金额 = 订单收入 − 订单调整（卖家自身成本为 0 时，引擎的 net 就是它）。"""
        self.assertAlmostEqual(
            self.result.net,
            self.components["order_income"] - OFFICIAL["order_adjustment"], places=6)
        self.assertAlmostEqual(self.result.net, 292.48, places=2)


class TheOnlyDifferenceTest(unittest.TestCase):
    """与官方算例的唯一差别是平台基础设施费——把它钉死，别让它悄悄变成别的。"""

    def test_the_gap_is_exactly_the_infrastructure_fee(self):
        result = quote()
        gap = OFFICIAL["order_income"] - result.components["order_income"]
        self.assertAlmostEqual(gap, OFFICIAL["infra_fee_my"], places=2)

    def test_zeroing_the_infrastructure_fee_reproduces_the_example_exactly(self):
        """把基础设施费按 0 计（即照官方算例的口径），其余逐项就能完全对上。

        这条同时说明：**差额不来自任何别的项**——费率、净运费、订单调整都已经一致。
        """
        spec = Spec.load(ROOT / "spec")
        engine = CostEngine(spec)
        result = engine.quote(CostInputs(
            market="MY", price_local=OFFICIAL["goods"], purchase_cny=0.0,
            domestic_cny=0.0, local_per_cny=1.0, seller_pays_freight=True,
            sls_freight=OFFICIAL["freight"], buyer_paid_freight=0.0,
            actual_commission=OFFICIAL["commission"], actual_txn_fee=OFFICIAL["txn_fee"],
            service_fee=OFFICIAL["service_fee"] - OFFICIAL["infra_fee_my"],
            service_fee_kind="service", withdraw_rate=0.0, fx_loss_rate=0.0,
            return_rate=0.0, order_adjustment=OFFICIAL["order_adjustment"],
            duty_rate=0.0, vat_rate=0.0))
        # 服务费按"纯服务费"填（去掉基础设施费那部分），基础设施费照样由引擎加
        self.assertEqual(result.status, COMPUTED, result.missing)
        # 引擎自己会加 MY 的 0.54，所以把服务费扣掉 0.54 正好还原官方口径
        self.assertAlmostEqual(result.components["order_income"],
                               OFFICIAL["order_income"], places=2)
        self.assertAlmostEqual(result.net, OFFICIAL["final"], places=2)


class OverlapWarningTest(unittest.TestCase):
    """服务费与基础设施费可能重叠——官方自己说过，所以必须提示而不是默认其一。"""

    def test_warning_appears_when_both_are_charged(self):
        result = quote()
        notes = [entry.note for entry in result.trace if "基础设施" in entry.label]
        self.assertTrue(notes)
        self.assertIn("重复", " ".join(notes), "两笔都收时要提示可能重复扣")

    def test_warning_also_appears_on_the_missing_fields_path(self):
        """缺字段是最常见的情形，那条路径的 trace 也必须带提示。

        这条是有来历的：提示最初只加在完整计算路径上，结果缺字段时看不到；
        补的时候又把它写在了 fee_trace 之后，直接 UnboundLocalError。
        """
        result = quote(purchase_cny=None)
        self.assertEqual(result.status, INCOMPLETE)
        notes = [entry.note for entry in result.trace if "基础设施" in entry.label]
        self.assertTrue(notes, "缺字段时也该看得到费用构成")
        self.assertIn("重复", " ".join(notes))

    def test_no_warning_when_no_service_fee(self):
        result = quote(service_fee=0.0)
        notes = " ".join(entry.note for entry in result.trace if "基础设施" in entry.label)
        self.assertNotIn("重复", notes)


class WorkedExampleIsRecordedTest(unittest.TestCase):
    def test_the_official_example_travels_with_the_repo(self):
        """算例本身要随仓库走——不然这条对账没法复核。"""
        import json

        path = ROOT / "spec" / "reference" / "order-settlement-structure.json"
        self.assertTrue(path.exists())
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("worked_example_my", doc)
        example = doc["worked_example_my"]
        for key, value in (("商品总额", OFFICIAL["goods"]),
                           ("订单收入", OFFICIAL["order_income"]),
                           ("最终金额", OFFICIAL["final"])):
            self.assertAlmostEqual(example[key], value, places=2)


if __name__ == "__main__":
    unittest.main()
