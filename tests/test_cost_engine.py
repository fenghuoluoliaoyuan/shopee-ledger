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


class CostEngineTest(unittest.TestCase):
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
