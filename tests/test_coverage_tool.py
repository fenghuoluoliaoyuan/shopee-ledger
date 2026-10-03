"""行覆盖率工具的测试。

这工具是自己写的（项目零依赖，装不了 coverage.py）。既然自己写，就得测——
而且我在这上面刚犯过两个错，正是这里守住的两条：

  1. ``co_lines()`` 返回 ``(起始字节, 结束字节, 行号)``，第一版把**字节偏移当行号**用，
     于是出现"第 0 行"，整个统计都错；
  2. 文件名比较没归一化，``co_filename`` 与 ``str(path)`` 形式不一致时一条都命中不了，
     把 profit.py 报成 0%。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.coverage import Seen, collect, executable_lines, summarize  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shopee_ledger"


class ExecutableLinesTest(unittest.TestCase):
    def test_line_numbers_are_line_numbers(self):
        """第一条就是行号 1，不是字节偏移 0。"""
        lines = executable_lines(PACKAGE / "profit.py")
        self.assertNotIn(0, lines, "lineno=0 是解释器的 RESUME，必须排除")
        self.assertEqual(min(lines), 1)

    def test_it_does_not_run_past_the_file(self):
        for path in sorted(PACKAGE.glob("*.py")):
            total = len(path.read_text(encoding="utf-8").splitlines())
            lines = executable_lines(path)
            self.assertLessEqual(max(lines), total, path.name)

    def test_blank_lines_and_comments_are_not_executable(self):
        with __import__("tempfile").TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            target = Path(folder) / "m.py"
            target.write_text("x = 1\n\n# 注释\n\n\ny = 2\n", encoding="utf-8")
            lines = executable_lines(target)
            self.assertIn(1, lines)
            self.assertIn(6, lines)
            for blank in (2, 3, 4, 5):
                self.assertNotIn(blank, lines)

    def test_a_syntax_error_gives_an_empty_set_not_a_crash(self):
        with __import__("tempfile").TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            target = Path(folder) / "bad.py"
            target.write_text("def broken(:\n", encoding="utf-8")
            self.assertEqual(executable_lines(target), set())


class SeenTest(unittest.TestCase):
    def test_filename_matching_is_normalised(self):
        """co_filename 的形式可能是相对/绝对、反斜杠/斜杠——不归一会一条都命中不了。"""
        target = PACKAGE / "profit.py"
        seen = Seen(prefix="shopee_ledger")
        seen.hits.add((str(target), 1))
        seen.hits.add((str(target).replace("\\", "/"), 2))
        seen.hits.add((str(target).upper(), 3))
        self.assertEqual(seen.lines_of(target), {1, 2, 3})

    def test_report_marks_a_module_that_was_never_imported(self):
        seen = Seen(prefix="shopee_ledger")
        report = {item["module"]: item for item in seen.report(PACKAGE)}
        self.assertEqual(report["profit.py"]["hit"], 0)
        self.assertGreater(report["profit.py"]["total"], 0)

    def test_summarize_reports_a_ratio(self):
        seen = Seen(prefix="shopee_ledger")
        text = summarize(seen.report(PACKAGE))
        self.assertIn("行覆盖率", text)


class CollectTest(unittest.TestCase):
    def test_collect_records_this_module(self):
        with collect(prefix="shopee_ledger") as seen:
            _ = executable_lines(PACKAGE / "profit.py")
        self.assertTrue(seen.hits, "with 块内至少应该记录到本模块的行")
        names = {name for name, _ in seen.hits}
        self.assertTrue(any("coverage" in name for name in names))

    def test_collect_cleans_up_the_tool_id(self):
        """退出 with 后必须释放工具 ID，否则同一进程第二次 collect 会失败。"""
        with collect(prefix="shopee_ledger"):
            pass
        with collect(prefix="shopee_ledger") as second:
            pass
        self.assertIsNotNone(second)

    def test_prefix_filter_excludes_other_files(self):
        with collect(prefix="shopee_ledger") as seen:
            _ = sum(range(10))
        for name, _ in seen.hits:
            self.assertIn("shopee_ledger", name.replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
