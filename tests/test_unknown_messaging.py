"""UNKNOWN 时的措辞：**不能把"还没算出来"说成"结论已成立"**。

这组测试守着一个真实缺陷：门禁在求值为 UNKNOWN 时，把 ``rule.message`` 原样输出。
而那些 message 是"判据成立时"的话，于是界面上出现了：

    ! [soft] R-COST-002  净利润率低于下限，砍掉          ← 净利润率其实是未知的
    ! [soft] R-COST-003  净利润率处于观察区间，建议补选或调整售价
    ? [hard] R-COST-005  净运费成本计算口径错误            ← 在说"我们自己的算式错了"

前两句互相矛盾，第三句更糟：R-COST-005 本来就是用来**检测我们算错**的规则，
缺数据时它却报告"口径错误"。

根因有两处，各自有测试：
  1. expr._compare 里 `except TypeError: return UNKNOWN` **不记是哪些字段**，
     于是调用方知道"算不出来"却不知道缺什么；
  2. gates 在 UNKNOWN 分支照抄 rule.message。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.cost_engine import COMPUTED, INCOMPLETE, CostEngine, CostInputs  # noqa: E402
from shopee_ledger.expr import UNKNOWN, evaluate  # noqa: E402
from shopee_ledger.gates import INCOMPLETE as GATE_INCOMPLETE  # noqa: E402
from shopee_ledger.gates import PASS, REJECT, GateService  # noqa: E402
from shopee_ledger.spec import Spec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class UnknownNamesTheFieldTest(unittest.TestCase):
    """字段存在但值是 None——与"字段根本不在 context"一样，都要报出名字。"""

    def test_none_value_is_reported(self):
        result = evaluate("net_margin < 0.10", {"net_margin": None})
        self.assertIs(result.value, UNKNOWN)
        self.assertIn("net_margin", result.unknowns)

    def test_missing_key_is_reported(self):
        result = evaluate("net_margin < 0.10", {})
        self.assertIn("net_margin", result.unknowns)

    def test_chained_comparison_reports_too(self):
        result = evaluate("0.10 <= net_margin < 0.15", {"net_margin": None})
        self.assertIn("net_margin", result.unknowns)

    def test_real_values_still_evaluate(self):
        """修 UNKNOWN 的报告不能把正常求值带偏。"""
        self.assertFalse(evaluate("net_margin < 0.10", {"net_margin": 0.31}).value)
        self.assertTrue(evaluate("net_margin < 0.10", {"net_margin": 0.05}).value)

    def test_none_in_a_compound_condition_is_named(self):
        result = evaluate("net_margin < 0.10 and price_local > 0",
                          {"net_margin": None, "price_local": 199.0})
        self.assertIs(result.value, UNKNOWN)
        self.assertIn("net_margin", result.unknowns)
        self.assertNotIn("price_local", result.unknowns, "有值的字段不该被报成缺")


class GateMessageTest(unittest.TestCase):
    """门禁在 UNKNOWN 时的措辞。"""

    @classmethod
    def setUpClass(cls):
        cls.spec = Spec.load(ROOT / "spec")
        cls.engine = CostEngine(cls.spec, today="2026-10-03")
        cls.gates = GateService(cls.spec, today="2026-10-03")

    def _outcomes(self, **overrides):
        base = dict(market="TW", price_local=199.0, purchase_cny=8.0, domestic_cny=1.5,
                    local_per_cny=4.49, sls_freight=55.0, buyer_paid_freight=0.0,
                    withdraw_rate=0.01, fx_loss_rate=0.005, return_rate=0.05)
        base.update(overrides)
        result = self.engine.quote(CostInputs(**base))
        gate = self.gates.check("G3", result.gate_context(), platform="shopee", market="TW")
        return result, gate, {item.rule_id: item for item in gate.outcomes}

    def test_missing_data_never_asserts_the_conclusion(self):
        _, gate, outcomes = self._outcomes(sls_freight=None)
        self.assertEqual(gate.result, GATE_INCOMPLETE)
        for rule_id in ("R-COST-002", "R-COST-003", "R-COST-005"):
            outcome = outcomes[rule_id]
            self.assertIn("判不了", outcome.message,
                          "%s 缺数据时不该断言结论：%s" % (rule_id, outcome.message))

    def test_the_alarming_wording_is_gone(self):
        """R-COST-005 是"检测我们算错"的规则，缺数据时不该说"口径错误"。"""
        _, _, outcomes = self._outcomes(sls_freight=None)
        self.assertNotIn("口径错误", outcomes["R-COST-005"].message)

    def test_the_contradictory_pair_is_gone(self):
        """R-COST-002 说"砍掉"、R-COST-003 说"处于观察区间"——不能同时当作结论出现。

        注意规则**名字**里本来就有"观察区间"这些词，所以不能简单禁词；
        要守的是「整句被框成未判定」而不是「原样输出判据成立时的话」。
        """
        _, _, outcomes = self._outcomes(sls_freight=None)
        messages = [outcomes[r].message for r in ("R-COST-002", "R-COST-003")]
        self.assertTrue(all(m.startswith("判不了") for m in messages), messages)
        # 原文是"判据成立时"的话，必须一字不差地不再出现
        for rule_id, raw in (("R-COST-002", "净利润率低于下限，砍掉"),
                             ("R-COST-003", "净利润率处于观察区间，建议补选或调整售价")):
            self.assertNotEqual(outcomes[rule_id].message, raw,
                                "%s 又把判据成立时的话当结论输出了" % rule_id)

    def test_the_message_names_what_is_missing(self):
        _, _, outcomes = self._outcomes(sls_freight=None)
        self.assertIn("net_margin", outcomes["R-COST-002"].message)

    def test_unknown_field_shows_up_in_the_missing_summary(self):
        """net_margin 以前不在缺字段汇总里——用户因此不知道要补什么。"""
        _, gate, _ = self._outcomes(sls_freight=None)
        self.assertIn("net_margin", gate.unknown_fields)

    def test_when_data_is_present_the_real_message_returns(self):
        """判据真的成立时，该说结论还是要说结论。"""
        _, gate, outcomes = self._outcomes(purchase_cny=200.0)
        outcome = outcomes["R-COST-002"]
        self.assertTrue(outcome.fired, "净利润率为负，这条应当触发")
        self.assertIn("砍掉", outcome.message)
        self.assertNotIn("判不了", outcome.message)

    def test_a_complete_quote_still_passes(self):
        result, gate, _ = self._outcomes()
        self.assertEqual(result.status, COMPUTED)
        self.assertEqual(gate.result, PASS, gate.explain())
        self.assertNotEqual(gate.result, REJECT)


if __name__ == "__main__":
    unittest.main()
