"""静态检查器「赋值前读取」的测试。

这个检查器存在的理由是两个真实的、连续两轮都出现的 bug：
  · 第 11 轮：函数内 import 撞模块级同名 → 同函数里靠后分支 UnboundLocalError
  · 第 12 轮：overlap_note 在 fee_trace 之后赋值 → 同样炸

两次都因为「那条分支从没被执行过」而活下来。测试覆盖不到的分支，
静态检查能覆盖——所以这里既测检查器本身，也把它当作全代码库的体检。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.unbound import check_file, check_package, check_source  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class CatchesTheHistoricalBugTest(unittest.TestCase):
    def test_overlap_note_shape(self):
        """第 12 轮的真实形态：赋值在后、读取在前。"""
        source = (
            "def quote():\n"
            "    trace = [overlap_note if overlap_note else '']\n"
            "    service = 6.26\n"
            "    overlap_note = ''\n"
            "    if service > 0:\n"
            "        overlap_note = '重复'\n"
            "    return trace\n"
        )
        findings = check_source(source, "round12.py")
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].name, "overlap_note")
        self.assertEqual(findings[0].read_line, 2)

    def test_assignment_inside_a_conditional_read_outside(self):
        source = (
            "def f(flag):\n"
            "    print(value)\n"
            "    if flag:\n"
            "        value = 1\n"
            "    return 0\n"
        )
        self.assertTrue(check_source(source, "x.py"))

    def test_import_in_a_later_block(self):
        source = (
            "def f(flag):\n"
            "    print(json.dumps({}))\n"
            "    if flag:\n"
            "        import json\n"
            "    return 0\n"
        )
        findings = check_source(source, "x.py")
        self.assertEqual([item.name for item in findings], ["json"])


class NoFalsePositivesTest(unittest.TestCase):
    """误报会让人把检查关掉，所以这些必须干净。"""

    def test_normal_order(self):
        self.assertEqual(check_source("def f():\n    x = 1\n    return x\n"), [])

    def test_parameter(self):
        self.assertEqual(check_source("def f(x):\n    return x\n"), [])

    def test_name_only_ever_read_is_global(self):
        """只读不赋值的名字是模块级/内置，不该报。"""
        self.assertEqual(check_source("def f():\n    return os.sep\n"), [])

    def test_loop_accumulator(self):
        source = ("def f(items):\n"
                  "    total = 0\n"
                  "    for i in items:\n"
                  "        total = total + i\n"
                  "    return total\n")
        self.assertEqual(check_source(source), [])

    def test_read_and_assign_inside_the_same_loop(self):
        source = ("def f(items):\n"
                  "    for i in items:\n"
                  "        print(later)\n"
                  "        later = i\n"
                  "    return 0\n")
        self.assertEqual(check_source(source), [], "同一循环内，第二次迭代就有值")

    def test_value_defined_in_every_branch(self):
        source = ("def f(flag):\n"
                  "    if flag:\n"
                  "        value = 1\n"
                  "    else:\n"
                  "        value = 2\n"
                  "    return value\n")
        self.assertEqual(check_source(source), [])

    def test_nested_function_scope_is_not_merged(self):
        source = ("def outer():\n"
                  "    def inner():\n"
                  "        return shell\n"
                  "    shell = 1\n"
                  "    return inner\n")
        self.assertEqual(check_source(source), [], "内层函数是另一个作用域")

    def test_augmented_assignment_before_use(self):
        self.assertEqual(check_source("def f():\n    n = 0\n    n += 1\n    return n\n"), [])

    def test_comprehension_target_is_not_a_late_assignment(self):
        """推导式的 for 写在元素之后，但**先**执行——按行号比会误判。

        这条是血的教训：第一版全代码库报了 50 条，**全是这一类**。
        误报 50 条比漏报更糟——会让人直接把检查关掉。
        """
        source = ("def f(items):\n"
                  "    return [\n"
                  "        item['id']\n"
                  "        for item in items\n"
                  "    ]\n")
        self.assertEqual(check_source(source), [])

    def test_dict_comprehension_with_two_targets(self):
        source = ("def f(mapping):\n"
                  "    return {\n"
                  "        key: value\n"
                  "        for key, value in mapping.items()\n"
                  "    }\n")
        self.assertEqual(check_source(source), [])

    def test_generator_expression_argument(self):
        source = ("def f(rows):\n"
                  "    return sum(\n"
                  "        row['n']\n"
                  "        for row in rows\n"
                  "    )\n")
        self.assertEqual(check_source(source), [])


class PackageIsCleanTest(unittest.TestCase):
    """全代码库体检——这条才是真正的防线。"""

    def test_no_read_before_assignment_anywhere(self):
        findings = check_package(ROOT / "shopee_ledger")
        self.assertEqual([item.render() for item in findings], [],
                         "存在「赋值前读取」，这些分支一旦被走到就会 UnboundLocalError")

    def test_the_checker_actually_sees_the_package(self):
        """防空跑：要是 glob 出问题，上面那条会永远通过。"""
        files = sorted((ROOT / "shopee_ledger").glob("*.py"))
        self.assertGreater(len(files), 20)
        findings = check_file(ROOT / "shopee_ledger" / "cost_engine.py")
        self.assertEqual(findings, [])

    def test_spec_scripts_are_clean_too(self):
        findings = check_file(ROOT / "spec" / "validate.py")
        self.assertEqual([item.render() for item in findings], [])


if __name__ == "__main__":
    unittest.main()
