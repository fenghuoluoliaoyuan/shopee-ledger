"""证据闭环：核实任务 → 带出处 → 参数升级 → 功能解锁。

这条链路是项目的核心承诺（"系统告诉你该核实什么，核实完功能自动开"），
所以每一步都要有测试钉住，尤其是"没凭据的 A 级"必须被拒。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.store import Ledger
from shopee_ledger.web import _checklist_post, checklist_page, spec_page


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.init()
        self.dts = next(row for row in self.ledger.checklist_rows()
                        if row["target_param_id"] == "P-TW-DTS")

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _row(self, item_id: int) -> dict:
        return next(row for row in self.ledger.checklist_rows() if row["id"] == item_id)

    # ---- 凭据纪律 -------------------------------------------------------
    def test_a_grade_without_url_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.ledger.set_checklist(self.dts["id"], "2026-10-03", "DTS 是 5 个工作日", "A")
        self.assertIn("URL", str(ctx.exception))

    def test_a_grade_with_url_is_accepted(self):
        self.ledger.set_checklist(self.dts["id"], "2026-10-03", "DTS 是 5 个工作日", "A",
                                  source_url="https://seller.test/dts")
        row = self._row(self.dts["id"])
        self.assertEqual(row["grade"], "A")
        self.assertEqual(row["source_url"], "https://seller.test/dts")
        self.assertTrue(row["conclusion"])

    def test_task_row_carries_target_and_evidence(self):
        self.assertIsNotNone(self.dts["target_param_id"])
        self.assertIn(self.dts["param_level"], ("A", "B", "C", "D", "E"))

    # ---- 闭环 -----------------------------------------------------------
    def test_unlock_disappears_after_verification(self):
        blocked = [item["param_id"] for item in self.ledger.unverified_unlocks()]
        self.assertIn("P-TW-DTS", blocked)
        self.assertFalse(self.ledger.dts_ready())

        self.ledger.set_param_value("P-TW-DTS", 5, "A",
                                    source_url="https://seller.test/dts",
                                    snapshot_ref="shots/dts.png")
        self.assertTrue(self.ledger.dts_ready(), "参数升级后倒计时功能应当启用")
        self.assertNotIn("P-TW-DTS",
                         [item["param_id"] for item in self.ledger.unverified_unlocks()])

    def test_web_post_records_and_upgrades_in_one_submit(self):
        result = unquote_plus(_checklist_post(self.ledger, {
            "id": [str(self.dts["id"])], "date": ["2026-10-03"], "grade": ["A"],
            "url": ["https://seller.test/dts"], "snapshot": ["shots/dts.png"],
            "conclusion": ["DTS=5 工作日，迟发判定 DTS+2"], "value": ["5"],
        }))
        self.assertIn("P-TW-DTS", result)
        self.assertIn("升到 A 级", result)
        self.assertTrue(self.ledger.dts_ready())
        row = self._row(self.dts["id"])
        self.assertTrue(row["conclusion"])
        self.assertEqual(row["snapshot_ref"], "shots/dts.png")

    def test_web_post_without_value_only_records(self):
        result = unquote_plus(_checklist_post(self.ledger, {
            "id": [str(self.dts["id"])], "date": ["2026-10-03"], "grade": ["D"],
            "url": [""], "snapshot": [""], "conclusion": ["公开页只写到 DTS 未明"], "value": [""],
        }))
        self.assertIn("已写入", result)
        self.assertFalse(self.ledger.dts_ready(), "没给值就不该动参数")

    def test_web_post_refuses_to_write_de_grade_into_override(self):
        result = unquote_plus(_checklist_post(self.ledger, {
            "id": [str(self.dts["id"])], "date": ["2026-10-03"], "grade": ["E"],
            "url": [""], "snapshot": [""], "conclusion": ["道听途说"], "value": ["5"],
        }))
        self.assertIn("未升级", result)
        self.assertFalse(self.ledger.dts_ready())

    # ---- 页面 -----------------------------------------------------------
    def test_checklist_page_lists_blocking_first_with_evidence_fields(self):
        page = checklist_page(self.ledger)
        self.assertIn("来源 URL", page)
        self.assertIn("截图 / 存档引用", page)
        self.assertIn("阻塞第一单", page)
        self.assertLess(page.index("阻塞第一单"), page.index("已核实") if "已核实" in page else len(page))

    def test_spec_page_shows_provenance_and_link(self):
        self.ledger.set_param_value("P-TW-DTS", 5, "A",
                                    source_url="https://seller.test/dts",
                                    snapshot_ref="shots/dts.png")
        page = spec_page(self.ledger)
        self.assertIn("seller.test/dts", page)
        self.assertIn("已核实 A 级", page)
        self.assertIn("shots/dts.png", page)


if __name__ == "__main__":
    unittest.main()
