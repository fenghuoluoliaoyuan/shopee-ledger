"""来源可达性记录的契约测试。

这一层守的是一条容易被忽略的规则：**A 级要求「可打开的 URL」**。
等级评上去了、URL 却打不开，那 A 级就只是记录，不是证据。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.reachability import (  # noqa: E402
    DEFAULT_PATH,
    load_reachability,
    record_reachability,
    unreachable_for,
)


class ReachabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "reachability.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_ok_and_broken(self):
        record_reachability(results=[("https://a.test", "HTTP 200"),
                                     ("https://b.test", "打不开：URLError")],
                            path=self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(data["urls"]["https://a.test"]["ok"])
        self.assertFalse(data["urls"]["https://b.test"]["ok"])
        self.assertIn("checked_at", data)

    def test_second_run_merges_and_overwrites_same_url(self):
        record_reachability(results=[("https://a.test", "打不开：URLError"),
                                     ("https://b.test", "HTTP 200")], path=self.path)
        record_reachability(results=[("https://a.test", "HTTP 200")], path=self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(data["urls"]["https://a.test"]["ok"], "同一 URL 要覆盖旧结论")
        self.assertIn("https://b.test", data["urls"], "没重查的 URL 要保留")

    def test_unreachable_helper_distinguishes_unknown(self):
        record_reachability(results=[("https://bad.test", "打不开：URLError")], path=self.path)
        records = load_reachability(self.path)
        self.assertTrue(unreachable_for("https://bad.test", records))
        self.assertIsNone(unreachable_for("https://never-checked.test", records),
                          "没记录过是未知，不等于可达")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(load_reachability(Path(self.tmp.name) / "nope.json"), {})

    def test_repo_record_exists_and_is_machine_readable(self):
        """仓库里那份记录要能读——/spec 与报表都会用它。"""
        if not DEFAULT_PATH.exists():
            self.skipTest("还没跑过 check-sources")
        data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
        self.assertIn("urls", data)
        for url, item in data["urls"].items():
            self.assertIn("ok", item, url)


if __name__ == "__main__":
    unittest.main()
