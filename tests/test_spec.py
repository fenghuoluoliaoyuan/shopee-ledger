"""spec 配置层、表达式求值器、门禁服务的契约测试。

这一组测试的作用：把「配置文法」和「四态门禁」变成可执行的契约。
特别是 test_conditions_reference_real_params 与 test_bare_names_are_declared，
它们能在加载期就抓出 spec 里写错的参数名——而不是等到线上永远返回 INCOMPLETE。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.expr import UNKNOWN, ExpressionError, evaluate, referenced
from shopee_ledger.gates import CONTEXT_KEYS, GateService, INCOMPLETE, PASS, REJECT, WARN
from shopee_ledger.spec import Spec, SpecError

ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec"


class SpecLoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = Spec.load(SPEC_ROOT)

    def test_counts_match_declared(self):
        # 54 = 原 52 + 两个曾被漏掉的真实平台费用（P-INFRA-FEE、P-TECH-FEE）
        # 58 = 54 + TH/MY 的佣金与交易手续费（来自官方定价模拟器接口）
        self.assertEqual(len(self.spec.params), 58)
        self.assertEqual(len(self.spec.rules), 46)
        self.assertEqual(len(self.spec.tasks), 48)
        self.assertEqual(len(self.spec.modes), 3)

    def test_the_two_missed_platform_fees_exist(self):
        """这两项是真实支出，掉了就会系统性高估利润。"""
        for param_id in ("P-INFRA-FEE", "P-TECH-FEE"):
            param = self.spec.params[param_id]
            self.assertEqual(param.evidence_level, "A", param_id)
            self.assertIn("by_market", param.value or {}, param_id)

    def test_no_self_check_problems(self):
        self.assertEqual(self.spec.problems, [], "\n".join(self.spec.problems))

    def _runtime_rules(self):
        """运行期规则。GOV 治理规则与自检规则的条件是散文，由 spec/validate.py 负责，不参与运行期求值。"""
        return [r for r in self.spec.rules.values() if r.raw.get("evaluated_by") != "validate.py"]

    def test_conditions_reference_real_params(self):
        """condition 里 params["X"] 的 X 必须是真实参数 id。"""
        bad = []
        for rule in self._runtime_rules():
            for pid in referenced(rule.condition)["params"]:
                if pid not in self.spec.params:
                    bad.append("%s: 引用了不存在的参数 %s" % (rule.id, pid))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_bare_names_are_declared(self):
        """condition 里的裸名字必须在 gates.CONTEXT_KEYS 词表内，且函数必须在白名单。"""
        bad = []
        for rule in self._runtime_rules():
            refs = referenced(rule.condition)
            for name in refs["names"]:
                if name not in CONTEXT_KEYS:
                    bad.append("%s: 未声明的上下文名 %r（condition: %s）" % (rule.id, name, rule.condition))
            for func in refs["funcs"]:
                if func not in ("match", "count", "suppliers", "max", "min", "abs", "len"):
                    bad.append("%s: 不在白名单的函数 %r" % (rule.id, func))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_every_condition_is_evaluable(self):
        """每个运行期 condition 都能被求值器处理（语法 + AST 白名单）。"""
        bad = []
        for rule in self._runtime_rules():
            try:
                evaluate(rule.condition, {})
            except ExpressionError as exc:
                bad.append("%s: %s" % (rule.id, exc))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_config_only_rules_are_marked(self):
        """GOV 门禁与自检规则必须显式标记为配置期，避免被误当运行期门禁。"""
        for rule in self.spec.rules.values():
            if rule.gate == "GOV" or rule.id == "R-LOG-008":
                self.assertEqual(
                    rule.raw.get("evaluated_by"), "validate.py",
                    "%s 应标记 evaluated_by=validate.py（它是配置期规则）" % rule.id,
                )

    def test_hard_rules_are_legally_hard(self):
        """硬规则要么依赖 A/B/C 级参数，要么声明了 on_degrade（INV-001）。"""
        for rule in self.spec.rules.values():
            if rule.level != "hard":
                continue
            for pid in rule.depends_on:
                param = self.spec.param(pid)
                if not param.hard_eligible() and not rule.on_degrade:
                    self.fail("%s 依赖 %s 且未声明 on_degrade" % (rule.id, pid))

    def test_unverified_claim_readers_are_advisory(self):
        for rule in self.spec.rules.values():
            if rule.reads_unverified_claim:
                self.assertNotEqual(rule.level, "hard", rule.id)
                self.assertTrue(rule.advisory_only, rule.id)

    def test_e_param_cannot_carry_value(self):
        from shopee_ledger.spec import Param

        with self.assertRaises(SpecError):
            Param.from_dict({"id": "X", "evidence_level": "E", "value": 1})

    def test_stale_detection(self):
        from shopee_ledger.spec import Param

        param = Param.from_dict({
            "id": "P-X", "evidence_level": "A", "value": 1, "state": "active",
            "next_review_at": "2020-01-01", "source": {"url": "u", "checked_at": "2020-01-01"},
        })
        self.assertTrue(param.is_stale("2026-10-03"))
        self.assertFalse(param.hard_eligible("2026-10-03"))
        self.assertEqual(param.effective_state("2026-10-03"), "stale")


class ExprTest(unittest.TestCase):
    def test_unknown_propagates_and_is_reported(self):
        result = evaluate("weight_g == null", {})
        self.assertTrue(result.is_unknown)
        self.assertEqual(result.unknowns, ["weight_g"])

    def test_false_wins_over_unknown_in_and(self):
        result = evaluate("missing_a && false", {})
        self.assertFalse(result.value)

    def test_true_wins_over_unknown_in_or(self):
        result = evaluate("missing_a || true", {})
        self.assertTrue(result.value)

    def test_unknown_does_not_become_false(self):
        result = evaluate("missing_a || missing_b", {})
        self.assertTrue(result.is_unknown)

    def test_param_path_and_lowercase_literals(self):
        context = {"params": {"P-TW-MARGIN-TH": {"value": {"warn": 0.10, "pass": 0.15}}}}
        self.assertTrue(evaluate("net_margin < P-TW-MARGIN-TH.value.warn", {"params": context["params"], "net_margin": 0.05}).value)
        self.assertFalse(evaluate("net_margin < P-TW-MARGIN-TH.value.warn", {"params": context["params"], "net_margin": 0.20}).value)
        self.assertTrue(evaluate("x == null", {"x": None}).value)
        self.assertTrue(evaluate("flag == true", {"flag": True}).value)

    def test_match_function(self):
        self.assertTrue(evaluate("match(title, terms)", {"title": "hello world", "terms": ["world"]}).value)
        self.assertFalse(evaluate("match(title, terms)", {"title": "abc", "terms": ["zzz"]}).value)
        self.assertTrue(evaluate("match(title, terms)", {"title": "abc", "terms": None}).is_unknown)

    def test_dotted_attribute_access(self):
        context = {"competitor": {"top20": {"discounted_same_items": 4}}}
        self.assertTrue(evaluate("competitor.top20.discounted_same_items >= 3", context).value)

    def test_expression_whitelist_blocks_dangerous_constructs(self):
        for bad in ("__import__('os')", "(1).__class__", "open('x')", "lambda: 1"):
            with self.assertRaises(ExpressionError):
                evaluate(bad, {})


class GateServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = Spec.load(SPEC_ROOT)
        cls.gates = GateService(cls.spec, today="2026-10-03")

    def test_missing_data_is_incomplete_not_reject(self):
        result = self.gates.check("G2", {"measurement": {}}, mode="dropship")
        self.assertIn(result.result, (INCOMPLETE, WARN))
        self.assertNotEqual(result.result, REJECT)

    def test_missing_weight_is_incomplete_with_field_name(self):
        result = self.gates.check("G2", {"measurement": {}}, mode="dropship")
        self.assertEqual(result.result, INCOMPLETE)
        self.assertIn("measurement.weight_g", result.unknown_fields)

    def test_missing_weight_fires_incomplete_result(self):
        result = self.gates.check("G2", {"measurement": {"weight_g": None}}, mode="dropship")
        fired = {item.rule_id: item for item in result.fired}
        self.assertIn("R-DATA-001", fired)
        self.assertEqual(fired["R-DATA-001"].result, INCOMPLETE)

    def test_d_level_threshold_degrades_instead_of_rejecting(self):
        """R-COST-002 是硬规则但依赖 D 级阈值 → 必须降级为 WARN，不得 REJECT（INV-001）。"""
        result = self.gates.check("G3", {"net_margin": 0.01, "sls_freight": 10}, platform="shopee", market="TW")
        outcome = {item.rule_id: item for item in result.outcomes}
        cost = outcome["R-COST-002"]
        self.assertTrue(cost.fired)
        self.assertEqual(cost.level, "soft")
        self.assertEqual(cost.result, WARN)
        self.assertIsNotNone(cost.degraded)

    def test_e_level_rule_never_rejects(self):
        result = self.gates.check("G2", {"weight_g": 7000}, platform="shopee", market="TW", mode="dropship")
        for item in result.outcomes:
            self.assertNotEqual(item.result, REJECT, item.rule_id)

    def test_unknown_claim_reader_stays_advisory(self):
        result = self.gates.check(
            "G2", {"is_liquid": True, "liquid_ml": 500}, platform="shopee", market="TW", mode="dropship"
        )
        outcome = {item.rule_id: item for item in result.outcomes}
        self.assertEqual(outcome["R-LOG-006"].level, "soft")
        self.assertIn(outcome["R-LOG-006"].result, (WARN, INCOMPLETE))

    def test_outcome_carries_full_contract(self):
        result = self.gates.check("G1", {"candidate": {"category": "apparel"}}, mode="dropship")
        payload = result.as_dict()
        self.assertIn("missing_fields", payload)
        for item in payload["outcomes"]:
            for key in ("rule_id", "result", "level", "action", "message", "next_action"):
                self.assertIn(key, item)

    def test_reject_path_is_reachable_for_code_level_rule(self):
        """R-SEL-002 依赖为空、证据 D 级但无参数依赖 → 保持 hard，可以 REJECT。"""
        result = self.gates.check("G1", {"candidate": {"category": "apparel"}}, mode="dropship")
        outcome = {item.rule_id: item for item in result.outcomes}
        self.assertEqual(outcome["R-SEL-002"].result, REJECT)
        self.assertEqual(result.result, REJECT)


if __name__ == "__main__":
    unittest.main()
