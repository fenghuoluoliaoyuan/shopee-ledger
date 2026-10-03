"""覆盖报告的分类与「已知打不开的来源」的处理。

这两件事都是为了让**失败信号保持可信**：
· 把已核实的参数报成「没找到线索」，会让人以为有缺口；
· 每天重试一个永久打不开的来源，会让每日任务恒返回失败码——
  "每天都失败"会训练人忽略失败。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.associate import coverage_report, needs_evidence  # noqa: E402
from shopee_ledger.sources import load_sources  # noqa: E402
from shopee_ledger.spec import Spec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class _Param:
    """最小替身：coverage_report 只用 value 与 evidence_level。"""

    def __init__(self, value, level):
        self.value = value
        self.evidence_level = level


class NeedsEvidenceTest(unittest.TestCase):
    def test_no_value_needs_evidence(self):
        self.assertTrue(needs_evidence(_Param(None, "A")))

    def test_d_and_e_need_evidence_even_with_a_value(self):
        self.assertTrue(needs_evidence(_Param(0.05, "D")))
        self.assertTrue(needs_evidence(_Param(0.05, "E")))

    def test_a_b_c_do_not(self):
        for level in ("A", "B", "C"):
            self.assertFalse(needs_evidence(_Param(0.14, level)), level)


class CoverageReportTest(unittest.TestCase):
    def test_splits_unverified_from_already_verified(self):
        params = {
            "P-A": _Param(0.14, "A"),      # 已核实，没线索 → 不算缺口
            "P-D": _Param(0.05, "D"),      # 经验值，没线索 → 缺口
            "P-E": _Param(None, "E"),      # 没值，没线索 → 缺口
        }
        report = coverage_report([], params)
        self.assertEqual(report["without_hits"], ["P-D", "P-E"])
        self.assertEqual(report["verified_without_hits"], ["P-A"])

    def test_params_with_hits_are_counted_once(self):
        class _Finding:
            param_id = "P-A"
            document_count = 3

        report = coverage_report([_Finding()], {"P-A": _Param(0.14, "A")})
        self.assertEqual(report["with_hits"], 1)
        self.assertEqual(report["documents"], 3)
        self.assertEqual(report["without_hits"], [])
        self.assertEqual(report["verified_without_hits"], [])

    def test_real_spec_keeps_the_two_buckets_apart(self):
        """对真实 spec 跑一遍：两桶都不该是空的，且互不重叠。"""
        spec = Spec.load(ROOT / "spec")
        report = coverage_report([], spec.params)
        self.assertTrue(report["without_hits"], "D/E 级参数应当被列出来")
        self.assertTrue(report["verified_without_hits"], "A/B/C 参数应当单独归类")
        overlap = set(report["without_hits"]) & set(report["verified_without_hits"])
        self.assertEqual(overlap, set())


class BlockedSourceTest(unittest.TestCase):
    """已知打不开的来源要标记，别让每日任务天天失败。"""

    def setUp(self):
        self.sources = load_sources()

    def test_the_unreachable_taiwan_source_is_marked_blocked(self):
        blocked = [s for s in self.sources if s.access == "blocked"]
        self.assertTrue(blocked, "help.shopee.tw 那条应当被标为 blocked")
        reason = blocked[0].blocked_reason
        self.assertTrue(reason, "跳过一个来源必须记下为什么——否则缺口被掩盖")
        self.assertIn("help.shopee.tw", reason)

    def test_blocked_reason_survives_loading(self):
        """加载时丢掉 blocked_reason 的话，命令行就只能打印一个光秃秃的 URL。"""
        blocked = [s for s in self.sources if s.access == "blocked"][0]
        self.assertIn("blocked_reason", blocked.as_dict())

    def test_still_public_sources_are_not_marked_blocked(self):
        for source in self.sources:
            if source.id == "SRC-TW-TAX-THRESHOLD":
                continue
            self.assertNotEqual(source.access, "blocked",
                                "%s 不该被标为打不开" % source.id)

    def test_only_known_access_values_are_used(self):
        allowed = {"public", "login", "blocked"}
        for source in self.sources:
            self.assertIn(source.access, allowed, source.id)


if __name__ == "__main__":
    unittest.main()
