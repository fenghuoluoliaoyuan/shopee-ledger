"""治理不变量的覆盖测试：让「我们有 14 条不变量」变成可验证的事实。

为什么需要
----------
``governance.json`` 声明了 14 条不变量，但**原先没有任何东西检查它们是否真被强制**。
实测发现 INV-004（INCOMPLETE 时 missing 必须非空）在源码与测试里都**无人提及**——
一条纸面不变量。声明而不强制比不声明更糟：它让人以为有保障。

这个文件做两件事：
1. 每条不变量都要登记它的**强制点**（validate.py 的检查 / 运行时守卫 / DB 触发器 / 行为测试）；
2. 强制点必须**可验证**——静态的查标记，行为的真跑一遍。
   没登记的不变量直接失败；确实放弃强制的要写进 WAIVED 并说明理由。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.cost_engine import COMPUTED, INCOMPLETE, CostEngine, CostInputs, CostResult
from shopee_ledger.spec import Spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec"


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 静态强制点：文件里必须出现这个标记（validate.py 的报错串）
# ---------------------------------------------------------------------------
STATIC = {
    "INV-001": ("spec/validate.py", "INV-001 违反"),
    "INV-002": ("spec/validate.py", "INV-002 违反"),
    "INV-003": ("spec/validate.py", "INV-003 待补"),
    "INV-005": ("spec/validate.py", "INV-005 违反"),
    "INV-008": ("shopee_ledger/store.py", "machine.history"),
    "INV-009": ("shopee_ledger/storage.py", "def fingerprint"),
    "INV-010": ("shopee_ledger/gates.py", "hard_eligible(self.today)"),
    "INV-011": ("spec/validate.py", "INV-011 违反"),
    "INV-012": ("spec/validate.py", "INV-012 违反"),
    "INV-013": ("spec/validate.py", "INV-013 违反"),
    "INV-014": ("spec/validate.py", "INV-014 违反"),
}


# ---------------------------------------------------------------------------
# 行为强制点：真跑一遍，看系统会不会拦
# ---------------------------------------------------------------------------
def inv004_incomplete_must_say_what_is_missing() -> bool:
    try:
        CostResult(INCOMPLETE)
    except ValueError:
        return True
    return False


def inv006_missing_is_not_zero() -> bool:
    """缺字段必须 INCOMPLETE，不能把 None 当 0 算出一个漂亮利润。"""
    spec = Spec.load(SPEC)
    result = CostEngine(spec).quote(CostInputs(market="TW", price_local=350.0))
    return result.status == INCOMPLETE and "purchase_cny" in result.missing


def inv007_immutable_tables_reject_updates() -> bool:
    """只增不改要**两层都挡**：Python 守卫挡常规调用，DB 触发器挡绕过它的裸 SQL。

    只测一层不够——实测 Python 层先抛 ValueError，触发器根本没被碰到。
    绕过守卫直接发 SQL 才能确认数据库自己也拦得住。
    """
    import sqlite3
    import tempfile

    from shopee_ledger.storage import Storage

    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        storage = Storage(Path(tmp.name) / "l.sqlite", Spec.load(SPEC))
        storage.init()
        row_id = storage.insert("AuditLog", {"action": "probe", "object_type": "x",
                                            "object_id": "1", "result": "PASS"})
        guarded = False
        try:
            storage.update("AuditLog", row_id, {"result": "TAMPERED"})
        except (ValueError, sqlite3.IntegrityError):
            guarded = True
        if not guarded:
            return False
        # 绕过 Python 守卫，看数据库自己拦不拦
        try:
            storage.connect().execute(
                "UPDATE audit_log SET result='TAMPERED' WHERE id=?", (row_id,))
            storage.connect().commit()
        except sqlite3.IntegrityError:
            return True
        return False
    finally:
        tmp.cleanup()


BEHAVIORAL = {
    "INV-004": inv004_incomplete_must_say_what_is_missing,
    "INV-006": inv006_missing_is_not_zero,
    "INV-007": inv007_immutable_tables_reject_updates,
}

# 明确放弃强制的（必须写理由）。留空表示 14 条全部有强制点。
WAIVED: dict[str, str] = {}


class GovernanceCoverageTest(unittest.TestCase):
    def setUp(self):
        import json

        self.invariants = json.loads((SPEC / "governance.json").read_text(encoding="utf-8"))["invariants"]
        self.ids = [item["id"] for item in self.invariants]

    def test_every_invariant_is_accounted_for(self):
        """没登记强制点的不变量直接失败——逼人要么强制它，要么明确放弃。"""
        registered = set(STATIC) | set(BEHAVIORAL) | set(WAIVED)
        missing = sorted(set(self.ids) - registered)
        self.assertEqual(missing, [], "这些不变量没有登记强制点：%s\n"
                                      "要么加检查/测试，要么写进 WAIVED 并说明理由" % missing)

    def test_no_stale_registrations(self):
        """登记了但 governance 里已经没有了——说明该更新，否则会掩盖真实条目。"""
        stale = sorted((set(STATIC) | set(BEHAVIORAL) | set(WAIVED)) - set(self.ids))
        self.assertEqual(stale, [], "登记表里有 governance.json 中不存在的不变量")

    def test_static_enforcement_markers_are_present(self):
        for invariant, (relative, marker) in STATIC.items():
            self.assertIn(marker, source(relative),
                          "%s 声称由 %s 强制，但那里找不到标记 %r" % (invariant, relative, marker))

    def test_behavioral_enforcement_actually_blocks(self):
        for invariant, probe in BEHAVIORAL.items():
            self.assertTrue(probe(), "%s 的行为强制点没起作用" % invariant)

    def test_waivers_carry_a_reason(self):
        for invariant, reason in WAIVED.items():
            self.assertTrue(reason and len(reason) > 10,
                            "%s 放弃了强制却没写清为什么" % invariant)


class SingleProfitImplementationTest(unittest.TestCase):
    """利润算式只许有一处。

    profit.py 里原本还有一套 v1.4 的引擎（ProfitInput/ProfitResult/evaluate），
    与 CostEngine 并行。生产路径早已不用它，但**两套公式并存本身就是隐患**：
    改一套忘另一套，两边会悄悄算出不同的数。删除后加这条守着，防止再写第二套。
    """

    def test_profit_module_no_longer_ships_an_engine(self):
        import shopee_ledger.profit as profit

        for name in ("ProfitInput", "ProfitResult", "evaluate"):
            self.assertFalse(hasattr(profit, name),
                             "profit.py 不该再有 %s——利润算式只有 cost_engine 一处" % name)

    def test_decision_enum_survives(self):
        from shopee_ledger.profit import Decision

        self.assertEqual({item.value for item in Decision},
                         {"go", "watch", "cut", "incomplete", "threshold_unset"})

    def test_only_cost_engine_defines_the_platform_fee_formula(self):
        """在源码里搜"佣金+手续费"这类算式，应当只在 cost_engine 出现。"""
        package = ROOT / "shopee_ledger"
        offenders = []
        for path in sorted(package.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "commission_amount + txn_amount" in text and path.name != "cost_engine.py":
                offenders.append(path.name)
        self.assertEqual(offenders, [], "利润算式出现在 cost_engine 之外：%s" % offenders)

    def test_no_module_imports_a_second_profit_engine(self):
        package = ROOT / "shopee_ledger"
        bad = []
        for path in sorted(package.glob("*.py")):
            if path.name == "profit.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "from shopee_ledger.profit import" in text and "Decision" not in text:
                bad.append(path.name)
        self.assertEqual(bad, [], "这些模块从 profit 导入了非 Decision 的东西：%s" % bad)


class Inv004Test(unittest.TestCase):
    """INV-004 曾经是纸面条款，现在在构造时就拦。"""

    def test_incomplete_without_missing_is_rejected(self):
        with self.assertRaises(ValueError):
            CostResult(INCOMPLETE)

    def test_incomplete_with_missing_is_fine(self):
        result = CostResult(INCOMPLETE, missing=["price_local"])
        self.assertEqual(result.status, INCOMPLETE)

    def test_computed_without_missing_is_fine(self):
        self.assertTrue(CostResult(COMPUTED, net=1.0, rate=0.1).computed)

    def test_real_engine_never_produces_an_explanation_free_incomplete(self):
        """跑几种缺数据的情形，每一种都必须说清缺什么。

        只用真实存在的市场——未知市场会直接抛 SpecError（那是另一回事：
        "市场不存在"不该退化成"数据不全"）。
        """
        spec = Spec.load(SPEC)
        engine = CostEngine(spec)
        cases = [
            CostInputs(market="TW"),
            CostInputs(market="TW", price_local=350.0),
            CostInputs(market="TW", price_local=350.0, purchase_cny=20.0),
            CostInputs(market="TW", price_local=350.0, purchase_cny=20.0,
                       domestic_cny=1.0, local_per_cny=4.5, sls_freight=60.0,
                       buyer_paid_freight=0.0),
            CostInputs(market="TH", price_local=500.0),
        ]
        for index, case in enumerate(cases):
            result = engine.quote(case)
            if result.status == INCOMPLETE:
                self.assertTrue(result.missing,
                                "第 %d 种情形 INCOMPLETE 却没说缺什么" % index)

    def test_unknown_market_is_an_error_not_an_incomplete(self):
        """「市场不存在」与「数据不全」要分开——前者是调用方用错了，不该被吞成 INCOMPLETE。"""
        from shopee_ledger.spec import SpecError

        with self.assertRaises(SpecError):
            CostEngine(Spec.load(SPEC)).quote(CostInputs(market="XX", price_local=100.0))


if __name__ == "__main__":
    unittest.main()
