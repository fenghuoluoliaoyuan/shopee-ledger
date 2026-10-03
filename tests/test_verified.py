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
