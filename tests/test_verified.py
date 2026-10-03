"""核实成果导出/回灌的契约测试。

守两条：
1. **真值随 git 走**——数据库是派生物，clone 下来不该丢核实成果。
2. 导出的是"当前生效状态"——同一参数多条历史覆盖只导最新那条。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.spec import default_spec  # noqa: E402
from shopee_ledger.verified import (  # noqa: E402
    DEFAULT_PATH,
    as_override_rows,
    build_verified,
    read_verified,
    write_verified,
)


def override(row_id, param_id, value, level="A", **extra):
    row = {"id": row_id, "param_id": param_id, "value": value, "evidence_level": level,
           "source_url": "https://example.test/%s" % param_id,
           "checked_at": "2026-10-0%d" % row_id, "snapshot_ref": "data/snapshots/x.txt"}
    row.update(extra)
    return row


class BuildVerifiedTest(unittest.TestCase):
    def test_keeps_only_the_latest_override_per_param(self):
        payload = build_verified([override(1, "P-X", 0.1, "E"),
                                  override(2, "P-X", 0.14, "A")], [])
        self.assertEqual(payload["params"]["P-X"]["value"], 0.14)
        self.assertEqual(payload["params"]["P-X"]["evidence_level"], "A")

    def test_order_of_input_rows_does_not_matter(self):
        forwards = build_verified([override(1, "P-X", 0.1), override(2, "P-X", 0.14)], [])
        backwards = build_verified([override(2, "P-X", 0.14), override(1, "P-X", 0.1)], [])
        self.assertEqual(forwards, backwards)

    def test_carries_the_provenance(self):
        payload = build_verified([override(1, "P-X", 0.14)], [])
        item = payload["params"]["P-X"]
        for key in ("source_url", "checked_at", "snapshot_ref", "evidence_level"):
            self.assertIn(key, item, "出处字段不能少：%s" % key)

    def test_only_finished_checklist_rows_are_exported(self):
        payload = build_verified([], [
            {"id": 1, "spec_task_id": "VT-1", "conclusion": "已核实", "grade": "A"},
            {"id": 2, "spec_task_id": "VT-2", "conclusion": "", "grade": None},
        ])
        ids = [item["spec_task_id"] for item in payload["checklist_done"]]
        self.assertEqual(ids, ["VT-1"])

    def test_empty_input_is_fine(self):
        payload = build_verified([], [])
        self.assertEqual(payload["params"], {})
        self.assertEqual(payload["checklist_done"], [])


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "verified.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_then_read(self):
        payload = build_verified([override(1, "P-X", 0.14)], [])
        write_verified(payload, self.path)
        self.assertEqual(read_verified(self.path)["params"]["P-X"]["value"], 0.14)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(read_verified(Path(self.tmp.name) / "nope.json"),
                         {"params": {}, "checklist_done": []})

    def test_corrupt_file_is_not_an_error(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(read_verified(self.path)["params"], {})

    def test_output_is_sorted_so_diffs_are_readable(self):
        payload = build_verified([override(1, "P-Z", 1), override(2, "P-A", 2),
                                  override(3, "P-M", 3)], [])
        write_verified(payload, self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertLess(text.index("P-A"), text.index("P-M"))
        self.assertLess(text.index("P-M"), text.index("P-Z"))


class PrecedenceTest(unittest.TestCase):
    """文件层与数据库层的优先级：**库里的后写覆盖要赢**。

    这曾经是个真 bug：两边 id 出自不同序列，撞车后文件反而赢了，
    页面上看到的还是旧值（被 test_pages_and_param_save 抓到）。
    """

    def test_file_rows_get_negative_ids(self):
        rows = as_override_rows({"params": {"P-A": {"value": 1}, "P-B": {"value": 2}}})
        self.assertTrue(all(row["id"] < 0 for row in rows), "文件行必须是负数 id")

    def test_database_row_wins_over_file_row(self):
        from shopee_ledger.spec import Spec

        spec = Spec.load(Path(__file__).resolve().parents[1] / "spec")
        file_rows = as_override_rows({"params": {"P-TW-COMMISSION": {
            "value": 0.14, "evidence_level": "A", "checked_at": "2026-01-01"}}})
        # 模拟库里后写的一条：id 是 autoincrement 的正数
        db_row = {"id": 7, "param_id": "P-TW-COMMISSION", "value": 0.20,
                  "evidence_level": "B", "checked_at": "2026-10-03"}
        merged = spec.with_overrides(file_rows + [db_row])
        self.assertAlmostEqual(merged.params["P-TW-COMMISSION"].value, 0.20, places=6,
                               msg="库里后写的覆盖必须赢")

    def test_file_row_applies_when_no_database_row(self):
        spec = default_spec()
        rows = as_override_rows({"params": {"P-TW-COMMISSION": {
            "value": 0.14, "evidence_level": "A"}}})
        merged = spec.with_overrides(rows)
        self.assertAlmostEqual(merged.params["P-TW-COMMISSION"].value, 0.14, places=6)

    def test_file_internal_order_is_preserved(self):
        rows = as_override_rows({"params": {"P-A": {"value": 1}, "P-B": {"value": 2}}})
        ids = [row["id"] for row in rows]
        self.assertEqual(ids, sorted(ids), "文件内部也要按升序，否则应用顺序会被打乱")


class VerifiedViewTest(unittest.TestCase):
    """用户可见的地方必须用**核实后的视图**，不能只读基础 spec。

    这曾经是个用户可见的 bug：配置页遍历 default_spec()，于是已核实的参数
    在页面上仍显示「未核实」——同一行里同时给出"已核实 A 级"和"未核实"，自相矛盾。
    quote2 更严重：拿基础 spec 算钱，用的是 E 级原值。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.tmp.name)
        payload = {"params": {
            "P-TW-SLS-TIERS": {"value": {"rate_date": "2026-10-03", "channels": {}},
                               "evidence_level": "A", "source_url": "https://example.test/sim",
                               "checked_at": "2026-10-03"},
        }, "checklist_done": []}
        self.path = self.folder / "verified.json"
        write_verified(payload, self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ledger_spec_sees_the_file_override(self):
        from shopee_ledger.store import Ledger

        ledger = Ledger(self.folder / "l.sqlite", verified_path=self.path)
        ledger.init()
        try:
            self.assertEqual(ledger.spec.params["P-TW-SLS-TIERS"].evidence_level, "A")
        finally:
            ledger.close()

    def test_disabling_the_file_restores_the_base_level(self):
        from shopee_ledger.store import Ledger

        ledger = Ledger(self.folder / "l.sqlite", verified_path=None)
        ledger.init()
        try:
            self.assertNotEqual(ledger.spec.params["P-TW-SLS-TIERS"].evidence_level, "A")
        finally:
            ledger.close()

    def test_spec_page_shows_the_verified_value_not_unverified(self):
        from shopee_ledger.store import Ledger
        from shopee_ledger.web import spec_page

        ledger = Ledger(self.folder / "l.sqlite", verified_path=self.path)
        ledger.init()
        try:
            html = spec_page(ledger)
            start = html.find("P-TW-SLS-TIERS")
            self.assertGreater(start, 0)
            row = html[start:start + 700]
            self.assertNotIn("未核实", row, "已核实的参数不该在配置页上显示未核实")
            self.assertIn("rate_date", row, "应当显示核实后的值")
        finally:
            ledger.close()

    def test_spec_page_without_the_file_still_renders(self):
        from shopee_ledger.store import Ledger
        from shopee_ledger.web import spec_page

        ledger = Ledger(self.folder / "l.sqlite", verified_path=None)
        ledger.init()
        try:
            self.assertIn("P-TW-SLS-TIERS", spec_page(ledger))
        finally:
            ledger.close()


class ApplyTest(unittest.TestCase):
    def test_rows_apply_onto_the_base_spec(self):
        payload = build_verified([override(1, "P-TW-COMMISSION", 0.14, "A")], [])
        merged = default_spec().with_overrides(as_override_rows(payload))
        self.assertEqual(merged.params["P-TW-COMMISSION"].evidence_level, "A")
        self.assertAlmostEqual(merged.params["P-TW-COMMISSION"].value, 0.14, places=6)

    def test_unknown_param_in_file_is_ignored(self):
        payload = {"params": {"P-NOT-REAL": {"value": 1, "evidence_level": "A"}}}
        merged = default_spec().with_overrides(as_override_rows(payload))
        self.assertNotIn("P-NOT-REAL", merged.params)

    def test_repo_file_exists_and_covers_the_worked_params(self):
        """仓库里那份导出必须真的在——它是我这轮核实成果的唯一 git 载体。"""
        self.assertTrue(DEFAULT_PATH.exists(), "跑 export-verified 生成 spec/verified.json")
        payload = read_verified(DEFAULT_PATH)
        for param_id in ("P-TW-COMMISSION", "P-TW-DTS"):
            self.assertIn(param_id, payload["params"])

    def test_repo_export_reproduces_a_level_without_the_database(self):
        """这条是这次修复的全部意义：不碰数据库，也能拿到核实过的等级。"""
        payload = read_verified(DEFAULT_PATH)
        merged = default_spec().with_overrides(as_override_rows(payload))
        for param_id in ("P-TW-DTS", "P-TW-COMMISSION", "P-SLS-BANNED"):
            self.assertEqual(merged.params[param_id].evidence_level, "A", param_id)

    def test_exported_json_is_loadable_and_ascii_safe(self):
        data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
        self.assertIn("params", data)
        self.assertTrue(data.get("note"))


if __name__ == "__main__":
    unittest.main()
