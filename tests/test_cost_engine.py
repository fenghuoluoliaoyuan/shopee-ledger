"""CostEngine 契约测试。

三条要锁住的语义：
1. 配置驱动——改 JSON 参数就能改结果，代码里没有 0.14/0.025 这类字面量。
2. 税负承担方——平台代收时不计入卖家成本，只记买家端价格上移。
3. 缺数据不冒 0——状态 INCOMPLETE 且列出缺哪个字段。
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.cost_engine import COMPUTED, INCOMPLETE, CostEngine, CostInputs
from shopee_ledger.gates import PASS, REJECT, WARN, GateService
from shopee_ledger.spec import Spec

ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec"


def tw_inputs(**overrides):
    data = dict(
        market="TW",
        price_local=350.0,
        purchase_cny=20.0,
        domestic_cny=1.5,
        local_per_cny=4.5,
        sls_freight=60.0,
        buyer_paid_freight=0.0,
        seller_pays_freight=True,
    )
    data.update(overrides)
    return CostInputs(**data)


class MissedPlatformFeesTest(unittest.TestCase):
    """两项以前被漏掉的真实平台费用：平台基础设施费、技术支持费。

    Shopee 自己的定价模拟器在页面上就写着「对于技术支持费上线的站点，请在定价时
    考虑相关费用」——spec 里原本一个都没有，成本模型会系统性高估利润。
    """

    @classmethod
    def setUpClass(cls):
        cls.spec = Spec.load(SPEC_ROOT)
        cls.engine = CostEngine(cls.spec, today="2026-10-03")

    def test_infra_fee_by_market_matches_the_official_table(self):
        self.assertAlmostEqual(self.engine.per_order_fee("VN").value, 3000.0, places=6)
        self.assertAlmostEqual(self.engine.per_order_fee("MY").value, 0.54, places=6)
        self.assertAlmostEqual(self.engine.per_order_fee("TH").value, 1.07, places=6)
        self.assertAlmostEqual(self.engine.per_order_fee("PH").value, 5.0, places=6)

    def test_unlisted_market_is_zero_with_a_reason_not_missing(self):
        """官方列表没列这个站点 → 该站点不收取，记 0。这和"未知"是两回事。"""
        rate = self.engine.per_order_fee("TW")
        self.assertAlmostEqual(rate.value, 0.0, places=6)
        self.assertIn("未列该站点", rate.source)

    def test_tech_fee_uses_the_tax_inclusive_rate(self):
        """越南官方示例：5% + 5%×8% = 5.4%，要用含税比例而不是 5%。"""
        rate = self.engine.tech_fee_rate("VN")
        self.assertAlmostEqual(rate.value, 0.054, places=6)
        self.assertIn("rate_incl_tax", rate.source)

    def test_tech_fee_unlisted_market_is_zero(self):
        self.assertAlmostEqual(self.engine.tech_fee_rate("TH").value, 0.0, places=6)

    def test_fee_markets_expose_which_markets_are_covered(self):
        self.assertEqual(self.engine.fee_markets("P-INFRA-FEE"), {"VN", "MY", "TH", "PH"})
        self.assertEqual(self.engine.fee_markets("P-TECH-FEE"), {"VN"})

    def test_quote_folds_both_fees_into_the_platform_fee(self):
        """把 TW 也塞进费用表，看钱有没有真的进 platform_fee。"""
        extra = {"P-INFRA-FEE": {"value": {"by_market": {"TW": 7.0}}, "evidence_level": "A"},
                 "P-TECH-FEE": {"value": {"by_market": {"TW": {"rate_incl_tax": 0.02}}},
                                "evidence_level": "A"}}
        from shopee_ledger.verified import as_override_rows

        spec = self.spec.with_overrides(as_override_rows({"params": extra}))
        result = CostEngine(spec).quote(tw_inputs())
        self.assertTrue(result.computed, result.missing)
        # 7.0 固定 + 350 × 2% = 7 元 → 共 14 进平台费
        self.assertAlmostEqual(result.components["infra_fee"], 7.0, places=6)
        self.assertAlmostEqual(result.components["tech_fee"], 7.0, places=6)
        self.assertIn("infra_fee", result.components)
        self.assertIn("技术支持费", [entry.label for entry in result.trace])

    def test_fee_trace_survives_an_incomplete_quote(self):
        """缺字段时更该看得见费用构成，而不是整块被吞掉。"""
        result = self.engine.quote(CostInputs(market="TH", price_local=500.0))
        self.assertEqual(result.status, INCOMPLETE)
        labels = [entry.label for entry in result.trace]
        self.assertIn("平台基础设施费", labels)
        self.assertIn("技术支持费", labels)

    def test_missing_param_is_not_silently_zero(self):
        """参数整个不存在时不能当成 0——那是另一类错误。"""
        engine = CostEngine(self.spec, today="2026-10-03")
        engine.spec = spec_without(self.spec, "P-INFRA-FEE")
        rate = engine.per_order_fee("TH")
        self.assertIsNone(rate.value)

    def test_listed_market_with_unreadable_value_counts_as_missing(self):
        """列表里有这个站点却取不到值 → 缺字段，不是 0。"""
        broken = {"P-INFRA-FEE": {"value": {"by_market": {"TH": None}}, "evidence_level": "A"}}
        from shopee_ledger.verified import as_override_rows

        spec = self.spec.with_overrides(as_override_rows({"params": broken}))
        result = CostEngine(spec).quote(CostInputs(market="TH", price_local=500.0))
        self.assertIn("infra_fee", result.missing)


def spec_without(spec, param_id):
    """去掉某个参数的 Spec 副本——用来测"参数不存在"的分支。"""
    import copy

    clone = copy.copy(spec)
    clone.params = {pid: param for pid, param in spec.params.items() if pid != param_id}
    return clone


class OfficialSettlementStructureTest(unittest.TestCase):
    """对齐官方订单结算口径（#25770 附录2）。

        订单收入 = 商品总额 - 运费总额 - 优惠券与回扣 - 各项费用
        最终金额 = 订单收入 - 订单调整

    以前引擎里没有"订单调整"这一项，马来西亚高价值商品税就无处安放。
    """

    @classmethod
    def setUpClass(cls):
        cls.spec = Spec.load(SPEC_ROOT)
        cls.engine = CostEngine(cls.spec, today="2026-10-03")

    def test_order_income_matches_the_official_formula(self):
        result = self.engine.quote(tw_inputs())
        self.assertTrue(result.computed, result.missing)
        components = result.components
        expected = (350.0 - components["net_freight"] - components["platform_fee"]
                    - components["affiliate"] - components["coupon_discount"])
        self.assertAlmostEqual(components["order_income"], expected, places=6)

    def test_order_income_is_exposed_even_when_zero_adjustment(self):
        result = self.engine.quote(tw_inputs())
        self.assertIn("order_income", result.components)
        self.assertEqual(result.components["order_adjustment"], 0.0)

    def test_order_adjustment_reduces_net_one_for_one(self):
        """订单调整（如马来高价值商品税）要原样从最终金额里扣掉。"""
        base = self.engine.quote(tw_inputs()).net
        taxed = self.engine.quote(tw_inputs(order_adjustment=121.80)).net
        self.assertAlmostEqual(base - taxed, 121.80, places=6)

    def test_order_adjustment_does_not_change_order_income(self):
        """订单调整是「订单收入之后」的一项，不该反过来影响订单收入。"""
        plain = self.engine.quote(tw_inputs()).components["order_income"]
        taxed = self.engine.quote(tw_inputs(order_adjustment=121.80)).components["order_income"]
        self.assertAlmostEqual(plain, taxed, places=6)

    def test_coupon_discount_reduces_order_income(self):
        plain = self.engine.quote(tw_inputs()).components["order_income"]
        discounted = self.engine.quote(tw_inputs(coupon_discount=50.0)).components["order_income"]
        self.assertAlmostEqual(plain - discounted, 50.0, places=6)

    def test_order_adjustment_shows_up_in_the_trace(self):
        result = self.engine.quote(tw_inputs(order_adjustment=121.80))
        labels = [entry.label for entry in result.trace]
        self.assertIn("订单调整", labels)
        self.assertIn("订单收入", labels)


class FreightByWeightTest(unittest.TestCase):
    """不填 sls_freight 时，用官方运费表按重量算。"""

    @classmethod
    def setUpClass(cls):
        cls.spec = Spec.load(SPEC_ROOT)
        cls.engine = CostEngine(cls.spec, today="2026-10-03")

    def test_engine_loads_the_freight_table_from_the_repo(self):
        self.assertIsNotNone(self.engine.freight_config, "仓库里应有运费表快照")
        self.assertEqual(self.engine.freight_config.get("date"), "2026-10-03")

    def test_weight_and_channel_produce_the_freight(self):
        result = self.engine.quote(tw_inputs(sls_freight=None, weight_g=1200.0,
                                             channel="蝦皮店到店"))
        self.assertNotIn("sls_freight", result.missing)
        self.assertAlmostEqual(result.context["sls_freight"], 95.0, places=6)

    def test_computed_freight_matches_a_hand_supplied_one(self):
        by_weight = self.engine.quote(tw_inputs(sls_freight=None, weight_g=1200.0,
                                                channel="蝦皮店到店"))
        by_hand = self.engine.quote(tw_inputs(sls_freight=95.0))
        self.assertAlmostEqual(by_weight.net, by_hand.net, places=6)

    def test_unknown_channel_falls_back_to_incomplete_not_zero(self):
        """表里查不到就必须 INCOMPLETE——把查不到当 0 会凭空多出利润。"""
        result = self.engine.quote(tw_inputs(sls_freight=None, weight_g=1200.0,
                                             channel="不存在的渠道"))
        self.assertEqual(result.status, INCOMPLETE)
        self.assertIn("sls_freight", result.missing)

    def test_missing_weight_is_incomplete(self):
        result = self.engine.quote(tw_inputs(sls_freight=None))
        self.assertEqual(result.status, INCOMPLETE)
        self.assertIn("sls_freight", result.missing)

    def test_special_cargo_costs_more(self):
        normal = self.engine.quote(tw_inputs(sls_freight=None, weight_g=1200.0,
                                             channel="蝦皮店到店", cargo="Normal"))
        special = self.engine.quote(tw_inputs(sls_freight=None, weight_g=1200.0,
                                              channel="蝦皮店到店", cargo="Special"))
        self.assertGreater(special.context["sls_freight"], normal.context["sls_freight"])

    def test_explicit_freight_wins_over_the_table(self):
        result = self.engine.quote(tw_inputs(sls_freight=11.0, weight_g=1200.0,
                                             channel="蝦皮店到店"))
        self.assertAlmostEqual(result.context["sls_freight"], 11.0, places=6)

    def test_engine_without_table_reports_missing_not_guess(self):
        engine = CostEngine(self.spec, today="2026-10-03", freight_config={})
        result = engine.quote(tw_inputs(sls_freight=None, weight_g=1200.0,
                                        channel="蝦皮店到店"))
        self.assertEqual(result.status, INCOMPLETE)
        self.assertIn("sls_freight", result.missing)


class CostEngineTestOld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = Spec.load(SPEC_ROOT)
        cls.engine = CostEngine(cls.spec, today="2026-10-03")

    # ---- 基础 -----------------------------------------------------------
    def test_tw_computes_with_param_rates(self):
        result = self.engine.quote(tw_inputs())
        self.assertEqual(result.status, COMPUTED)
        self.assertIsNotNone(result.rate)
        self.assertEqual(result.missing, [])

    def test_net_equals_sum_of_components(self):
        """自洽性：net 必须等于按成分重算的结果（防公式漂移）。"""
        result = self.engine.quote(tw_inputs())
        expected = (
            result.components["purchase_local"] + result.components["domestic_local"]
            + result.components["net_freight"] + result.components["platform_fee"]
            + result.components["affiliate"] + result.components["ad_spend"]
            + result.components["withdraw"] + result.components["fx_loss"]
            + result.components["return_reserve"] + result.components["import_tax"]
            + result.net
        )
        self.assertAlmostEqual(expected, tw_inputs().price_local, places=6)

    def test_net_freight_only_charges_excess(self):
        seller = self.engine.quote(tw_inputs(seller_pays_freight=True, buyer_paid_freight=0))
        self.assertAlmostEqual(seller.components["net_freight"], 60.0)
        partial = self.engine.quote(tw_inputs(seller_pays_freight=False, buyer_paid_freight=25.0))
        self.assertAlmostEqual(partial.components["net_freight"], 35.0)
        over = self.engine.quote(tw_inputs(seller_pays_freight=False, buyer_paid_freight=99.0))
        self.assertAlmostEqual(over.components["net_freight"], 0.0, msg="买家多付不退，不得变成收入")

    def test_free_window_zeroes_commission_but_keeps_txn_fee(self):
        normal = self.engine.quote(tw_inputs())
        free = self.engine.quote(tw_inputs(in_free_window=True))
        normal_fee = normal.components["platform_fee"]
        free_fee = free.components["platform_fee"]
        self.assertLess(free_fee, normal_fee, "免佣窗口内平台费必须下降")
        # 差额应恰为佣金（14% × 350）
        self.assertAlmostEqual(normal_fee - free_fee, 350 * 0.14, places=6)
        self.assertGreater(free_fee, 0, "交易手续费在免佣窗口内照收")

    def test_service_fee_only_when_kind_is_service(self):
        with_service = self.engine.quote(tw_inputs(service_fee=10.0, service_fee_kind="service"))
        as_shipping = self.engine.quote(tw_inputs(service_fee=10.0, service_fee_kind="shipping"))
        self.assertAlmostEqual(
            with_service.components["platform_fee"] - as_shipping.components["platform_fee"], 10.0
        )

    # ---- 缺数据不冒 0 ---------------------------------------------------
    def test_missing_sls_is_incomplete(self):
        result = self.engine.quote(tw_inputs(sls_freight=None))
        self.assertEqual(result.status, INCOMPLETE)
        self.assertIn("sls_freight", result.missing)

    def test_missing_fx_is_incomplete(self):
        result = self.engine.quote(tw_inputs(local_per_cny=None))
        self.assertEqual(result.status, INCOMPLETE)
        self.assertIn("local_per_cny", result.missing)

    def test_unknown_market_rates_are_incomplete_not_zero(self):
        """BR 无任何参数文件 → 必须 INCOMPLETE，绝不能把缺失费率当 0。"""
        result = self.engine.quote(tw_inputs(market="BR"))
        self.assertEqual(result.status, INCOMPLETE)
        self.assertIn("commission_rate", result.missing)

    # ---- 税负承担方（本轮新增的核心语义） --------------------------------
    def test_th_withholding_is_not_a_seller_cost(self):
        result = self.engine.quote(tw_inputs(
            market="TH", actual_commission=49.0, actual_txn_fee=8.75,
            withdraw_rate=0.01, fx_loss_rate=0.005, return_rate=0.05,
        ))
        self.assertEqual(result.status, COMPUTED)
        self.assertAlmostEqual(result.components["import_tax"], 0.0,
                               msg="平台代收时不得计入卖家成本")
        self.assertIsNotNone(result.buyer_price_uplift)
        self.assertAlmostEqual(result.buyer_price_uplift, 0.17, places=6)
        self.assertTrue(any("买家端价格上移" in entry.label for entry in result.trace))

    def test_scope_isolation_market_rates_are_not_reused(self):
        """台湾的提现费率带 scope.market=TW，不得被套用到泰国（INV-012）。"""
        result = self.engine.quote(tw_inputs(
            market="TH", actual_commission=49.0, actual_txn_fee=8.75,
        ))
        self.assertEqual(result.status, INCOMPLETE)
        self.assertIn("withdraw_rate", result.missing)

    def test_tw_intact_policy_has_no_import_tax_line(self):
        result = self.engine.quote(tw_inputs())
        self.assertAlmostEqual(result.components["import_tax"], 0.0)
        self.assertIsNone(result.buyer_price_uplift)

    # ---- 配置驱动 -------------------------------------------------------
    def test_changing_param_json_changes_result(self):
        with tempfile.TemporaryDirectory() as folder:
            temp_spec = Path(folder) / "spec"
            shutil.copytree(SPEC_ROOT, temp_spec)
            path = temp_spec / "params" / "platform.shopee.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            for param in doc["params"]:
                if param["id"] == "P-TW-COMMISSION":
                    param["value"] = 0.30
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

            before = CostEngine(self.spec).quote(tw_inputs())
            after = CostEngine(Spec.load(temp_spec)).quote(tw_inputs())
            self.assertLess(after.rate, before.rate, "把佣金从 14% 改成 30%，净利润率必须下降")
            self.assertAlmostEqual(
                after.components["platform_fee"] - before.components["platform_fee"], 350 * 0.16, places=6
            )
            self.assertAlmostEqual(after.components["commission"], 350 * 0.30, places=6)

    # ---- 证据链 ---------------------------------------------------------
    def test_trace_carries_sources_and_levels(self):
        result = self.engine.quote(tw_inputs())
        labels = {entry.label: entry for entry in result.trace}
        self.assertIn("佣金", labels)
        self.assertIn("退货预留", labels)
        self.assertTrue(any("P-TW" in entry.source or "P-RETURN" in entry.source for entry in result.trace))

    def test_explain_renders_without_error(self):
        text = self.engine.quote(tw_inputs()).explain()
        self.assertIn("核算结果", text)
        self.assertIn("平台费", text)


class EngineGateIntegrationTest(unittest.TestCase):
    """引擎算数 → 门禁决策，这条链路必须真正打通（否则 G3 永远 INCOMPLETE）。"""

    @classmethod
    def setUpClass(cls):
        cls.spec = Spec.load(SPEC_ROOT)
        cls.engine = CostEngine(cls.spec, today="2026-10-03")
        cls.gates = GateService(cls.spec, today="2026-10-03")

    def test_healthy_quote_passes_g3(self):
        result = self.engine.quote(tw_inputs())
        gate = self.gates.check("G3", result.gate_context(), platform="shopee", market="TW")
        self.assertEqual(gate.result, PASS, gate.explain())
        self.assertEqual(gate.fired, [])

    def test_low_margin_degrades_to_warn_not_reject(self):
        """净利润率为负 → R-COST-002 触发，但阈值是 D 级，必须降级为 WARN。"""
        result = self.engine.quote(tw_inputs(purchase_cny=60.0))
        self.assertLess(result.rate, 0.10)
        gate = self.gates.check("G3", result.gate_context(), platform="shopee", market="TW")
        outcome = {item.rule_id: item for item in gate.outcomes}
        self.assertTrue(outcome["R-COST-002"].fired)
        self.assertEqual(outcome["R-COST-002"].result, WARN)
        self.assertEqual(gate.result, WARN)

    def test_incomplete_quote_makes_gate_incomplete_not_reject(self):
        result = self.engine.quote(tw_inputs(sls_freight=None))
        self.assertEqual(result.status, INCOMPLETE)
        gate = self.gates.check("G3", result.gate_context(), platform="shopee", market="TW")
        self.assertEqual(gate.result, INCOMPLETE)
        self.assertNotEqual(gate.result, REJECT)
        outcome = {item.rule_id: item for item in gate.outcomes}
        self.assertEqual(outcome["R-COST-001"].result, INCOMPLETE)

    def test_gate_never_sees_unknown_when_quote_is_complete(self):
        """核算完整时，G3 不应再出现 UNKNOWN 字段——这是两条链路接通的判据。"""
        result = self.engine.quote(tw_inputs(is_presale=False))
        gate = self.gates.check("G3", result.gate_context(), platform="shopee", market="TW")
        self.assertEqual(gate.unknown_fields, [], gate.explain())

    def test_non_intact_market_never_treated_as_exempt(self):
        """已取消低值免税的市场，引擎绝不把进口税当 exempt（防止结构性高估利润）。"""
        for market in ("TH", "BR"):
            result = self.engine.quote(tw_inputs(
                market=market, actual_commission=49.0, actual_txn_fee=8.75,
                withdraw_rate=0.01, fx_loss_rate=0.005, return_rate=0.05,
            ))
            treatment = result.context.get("import_tax_treatment")
            self.assertNotEqual(treatment, "exempt", "%s 不得标为免税" % market)

    def test_th_buyer_uplift_raises_advisory(self):
        result = self.engine.quote(tw_inputs(
            market="TH", actual_commission=49.0, actual_txn_fee=8.75,
            withdraw_rate=0.01, fx_loss_rate=0.005, return_rate=0.05,
        ))
        gate = self.gates.check("G3", result.gate_context(), platform="shopee", market="TH", mode="dropship")
        outcome = {item.rule_id: item for item in gate.outcomes}
        self.assertTrue(outcome["R-MKT-002"].fired, gate.explain())
        self.assertEqual(outcome["R-MKT-002"].result, WARN)


if __name__ == "__main__":
    unittest.main()
