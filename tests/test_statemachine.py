"""状态机契约测试：顺序、守卫、异常分支，外加 spec 自身的完整性。

最后这一类最值钱——状态机定义写在 JSON 里，`validate.py` 管不到图论性质：
少写一条转移就会产生"永远到不了的状态"，这里用可达性检查抓它。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.fulfillment import (
    Fulfillment,
    advance,
    arrange_ship,
    copy_address,
    mark_stock,
    order_context,
)
from shopee_ledger.spec import Spec, default_spec
from shopee_ledger.statemachine import OrderMachine, TransitionError

SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"


class MachineLoadTest(unittest.TestCase):
    def setUp(self):
        self.spec = Spec.load(SPEC_ROOT)
        self.machine = OrderMachine(self.spec)

    def test_states_come_from_spec(self):
        definition = self.spec.mode("dropship")["order_state_machine"]
        self.assertEqual(self.machine.states, definition["states"])
        self.assertEqual(self.machine.initial, definition["initial"])
        self.assertEqual(len(self.machine.states), 13)
        self.assertEqual(self.machine.state, definition["initial"])

    def test_no_duplicate_transitions(self):
        pairs = [(t.from_state, t.to_state) for t in self.machine.transitions]
        self.assertEqual(len(pairs), len(set(pairs)), "转移表有重复项")

    def test_every_transition_endpoint_is_a_declared_state(self):
        for item in self.machine.transitions:
            self.assertIn(item.from_state, self.machine.states)
            self.assertIn(item.to_state, self.machine.states)

    def test_every_state_is_reachable_from_initial(self):
        seen = {self.machine.initial}
        frontier = [self.machine.initial]
        while frontier:
            current = frontier.pop()
            for item in self.machine.transitions:
                if item.from_state == current and item.to_state not in seen:
                    seen.add(item.to_state)
                    frontier.append(item.to_state)
        self.assertEqual(set(self.machine.states) - seen, set(), "存在不可达状态")

    def test_final_state_has_no_exit(self):
        self.assertEqual(OrderMachine(self.spec, state="paid").allowed(), [])

    def test_declared_rules_exist_in_spec(self):
        for item in self.machine.transitions:
            for rule_id in item.rules:
                self.assertIn(rule_id, self.spec.rules,
                              "%s→%s 引用了不存在的规则 %s" % (item.from_state, item.to_state, rule_id))


class TransitionTest(unittest.TestCase):
    def setUp(self):
        self.machine = OrderMachine(default_spec())

    def test_allowed_from_created(self):
        self.assertEqual(sorted(self.machine.allowed()), ["cancelled", "stock_checked"])

    def test_fire_moves_and_records_history(self):
        record = self.machine.fire("stock_checked")
        self.assertEqual(self.machine.state, "stock_checked")
        self.assertEqual(record["from"], "created")
        self.assertEqual(record["to"], "stock_checked")
        self.assertEqual(len(self.machine.history), 1)

    def test_skip_is_refused_and_lists_what_is_allowed(self):
        with self.assertRaises(TransitionError) as ctx:
            self.machine.fire("po_created")
        message = str(ctx.exception)
        self.assertIn("不允许", message)
        self.assertIn("stock_checked", message, "报错必须告诉人当前能去哪")

    def test_final_state_cannot_move(self):
        machine = OrderMachine(default_spec(), state="paid")
        with self.assertRaises(TransitionError):
            machine.fire("cancelled")

    def test_unknown_state_refused(self):
        with self.assertRaises(TransitionError):
            OrderMachine(default_spec(), state="查无此态")

    def test_cancel_branch_and_ex03(self):
        self.machine.fire("cancelled")
        self.assertEqual(self.machine.state, "cancelled")
        self.assertTrue(any(a["id"] == "EX-03" for a in self.machine.exceptions()))

    def test_guard_rules_attached_to_the_right_transition(self):
        self.machine.fire("stock_checked")
        self.assertEqual(self.machine.transition_rules("ship_arranged"), ["R-ORDER-001"])
        self.assertEqual(self.machine.transition_rules("cancelled"), [])

    def test_ex01_fires_after_24h_in_ship_arranged(self):
        self.machine.fire("stock_checked")
        self.machine.fire("ship_arranged")
        self.assertTrue(any(a["id"] == "EX-01" for a in self.machine.exceptions(hours_in_state=30)))
        self.assertEqual(self.machine.exceptions(hours_in_state=2), [])


class GuardIntegrationTest(unittest.TestCase):
    """守卫规则必须真的被执行，而不只是写在 JSON 里。"""

    def _stocked(self) -> Fulfillment:
        state = Fulfillment()
        mark_stock(state, 3, True, True, False)
        return state

    def test_arrange_passes_guard_once_stock_checked(self):
        state = self._stocked()
        arrange_ship(state)
        self.assertEqual(state.state, "ship_arranged")

    def test_guard_context_is_filled_by_the_machine(self):
        state = self._stocked()
        context = order_context(state)
        self.assertIsNotNone(context["order"]["stock_checked_at"],
                             "缺这个字段会把硬规则卡成 INCOMPLETE，导致所有转移都过不去")

    def test_skip_is_still_refused_at_the_adapter_level(self):
        state = self._stocked()
        with self.assertRaises(ValueError) as ctx:
            advance(state, "po_created")
        self.assertIn("不允许", str(ctx.exception))

    def test_purchase_guard_blocks_without_order_page_address(self):
        state = self._stocked()
        arrange_ship(state)
        advance(state, "address_captured")           # 结构上合法
        with self.assertRaises(ValueError) as ctx:   # 但没有当单地址 → 守卫拦下
            advance(state, "po_created")
        self.assertIn("守卫未通过", str(ctx.exception))
        self.assertEqual(state.state, "address_captured", "守卫失败不得推进状态")

    def test_purchase_passes_after_address_copied(self):
        state = self._stocked()
        arrange_ship(state)
        copy_address(state, "当单中转仓 订单号SN1", "order_page")
        advance(state, "po_created")
        self.assertEqual(state.state, "po_created")


if __name__ == "__main__":
    unittest.main()
