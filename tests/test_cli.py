"""CLI 冒烟测试：**每条分支都要被真的跑到**。

为什么必须有这个文件
--------------------
delivery 分支里我写了 `from datetime import date as _date`，而模块顶层是
`import datetime as _date`。Python 的规则是"函数里只要对这个名字有一次赋值，
它就是全程局部变量"——于是同一个 _run 里靠后的 freight 分支一访问就
UnboundLocalError，**`freight --check` 直接崩**，而从测试到每日任务都没发现
（每日任务那次刚好没跑到？不，是它跑了但退出码被容忍了；真正的原因是没有任何
测试碰过 CLI 分支）。498 个测试全绿，却没有一个调用过 freight 命令。

这类"整条分支从没被执行过"的问题，靠逐个补单元测试是补不完的。
所以这里按命令表把每条分支都过一遍，只要求一件事：**不抛未处理异常**。
退出码允许 0 或 1（有的分支在空库上就是该报错），但 traceback 一律算失败。
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.__main__ import main  # noqa: E402

# (参数, 说明)。都只碰本地：不联网、不开浏览器、不写 spec/。
LOCAL_COMMANDS: list[tuple[list[str], str]] = [
    (["init"], "建库"),
    (["spec-info"], "配置概览"),
    (["checklist"], "待核实清单"),
    (["alert"], "告警"),
    (["calibrate"], "KPI 校准"),
    (["check-snapshots"], "快照体检"),
    (["manual"], "待读数留档"),
    (["landed", "--purchase", "20", "--domestic", "2", "--weight-g", "500"],
     "多站点落地对比"),
    (["associate"], "文档线索关联（空库上会提示先跑 harvest）"),
    (["delivery", "--verify"], "发货时效规则回验"),
    (["delivery", "--market", "TW", "--order-date", "2026-10-15", "--dts", "1"],
     "算一个订单的截止"),
    (["delivery", "--market", "TH", "--order-date", "2026-10-15"],
     "备货时长默认值"),
    (["freight"], "运费表（读仓库快照）"),
    (["survival", "--site", "TW"], "存活判定"),
    (["review"], "待确认候选"),
    (["param-list", "--site", "TW"], "参数列表"),
    (["import-verified", "--dry-run"], "导入核实成果（只看不写）"),
]

# 需要交互式参数的命令单独测，避免把表塞太满
WITH_ARGS: list[tuple[list[str], str]] = [
    (["supplier-add", "--name", "甲", "--dropship", "yes", "--address-ok", "yes"], "加供应商"),
    (["candidate-add", "--site", "TW", "--name", "杯垫", "--weight", "120",
      "--purchase", "8", "--domestic", "1.5", "--price", "199", "--sls", "55"], "加候选品"),
]


class CliSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.tmp.name) / "cli.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, args: list[str]) -> tuple[int, str]:
        """跑一条命令，返回 (退出码, 输出)。未处理异常直接让测试失败。"""
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", self.db] + args)
        except SystemExit as exc:          # argparse 的正常退出
            code = int(exc.code or 0)
        except Exception as exc:           # 未处理异常 = 测试失败
            self.fail("`%s` 抛了未处理异常 %s: %s\n%s"
                      % (" ".join(args), type(exc).__name__, exc, stderr.getvalue()[-500:]))
        return code, stdout.getvalue() + stderr.getvalue()

    def test_every_local_command_runs_without_crashing(self):
        self._run(["init"])
        for args, label in LOCAL_COMMANDS:
            if args == ["init"]:
                continue
            with self.subTest(command=" ".join(args), what=label):
                code, output = self._run(args)
                self.assertIn(code, (0, 1),
                              "`%s` 退出码 %d\n%s" % (" ".join(args), code, output[-400:]))

    def test_stateful_commands_run_without_crashing(self):
        self._run(["init"])
        for args, label in WITH_ARGS:
            with self.subTest(command=" ".join(args), what=label):
                code, output = self._run(args)
                self.assertIn(code, (0, 1),
                              "`%s` 退出码 %d\n%s" % (" ".join(args), code, output[-400:]))
        # 有数据之后再走一遍：分支里有些路径只在有数据时才到
        follow_ups = [(["quote", "--id", "1"], "报价"),
                      (["order-open", "--candidate", "1"], "开单"),
                      (["order-next", "--order", "1"], "下一步"),
                      (["checklist"], "待核实"),
                      (["alert"], "告警")]
        for args, label in follow_ups:
            with self.subTest(command=" ".join(args), what=label):
                code, output = self._run(list(args))
                self.assertIn(code, (0, 1),
                              "`%s` 退出码 %d\n%s" % (" ".join(args), code, output[-400:]))

    def test_missing_required_flag_is_a_clean_error_not_a_traceback(self):
        code, output = self._run(["delivery"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", output)

    def test_unknown_market_is_rejected_cleanly(self):
        """站点拼错必须报错：只有 TH/BR/AR 走"发货日"口径，其他走自然日——
        静默套用另一种规则会算出一个看起来正常其实错的日期。"""
        code, output = self._run(["delivery", "--market", "ZZ", "--order-date", "2026-10-15"])
        self.assertEqual(code, 1)
        self.assertIn("未知站点", output)
        self.assertNotIn("Traceback", output)

    def test_markets_outside_the_active_dimension_still_compute(self):
        """计算器覆盖 9 站，比项目的 5 个市场维度宽——不该因为维度没扩就不让算。"""
        code, output = self._run(["delivery", "--market", "PH", "--order-date", "2026-10-15"])
        self.assertEqual(code, 0)
        self.assertIn("PH 站", output)
        self.assertIn("不在项目当前的市场维度", output)

    def test_no_function_level_import_shadows_a_module_level_name(self):
        """回归防线：函数内的 import 会把该名字变成整个函数的局部变量。

        delivery 分支里 `from datetime import date as _date` 撞上模块顶层的
        `import datetime as _date`，把同函数里靠后的 freight 分支打崩了。
        这条静态检查能在写下的那一刻就发现，而不用等某条分支被跑到。
        """
        import ast

        source = (Path(__file__).resolve().parents[1]
                  / "shopee_ledger" / "__main__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_names = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                module_names.update(a.asname or a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                module_names.update(a.asname or a.name for a in node.names)

        clashes = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import):
                    names = [a.asname or a.name.split(".")[0] for a in inner.names]
                elif isinstance(inner, ast.ImportFrom):
                    names = [a.asname or a.name for a in inner.names]
                else:
                    continue
                for name in names:
                    if name in module_names:
                        clashes.append("%s() 里 import 的 %s 撞上了模块级同名"
                                       % (node.name, name))
        self.assertEqual(sorted(set(clashes)), [],
                         "函数内 import 与模块级名字冲突——该名字会变成全程局部变量：%s"
                         % sorted(set(clashes)))


if __name__ == "__main__":
    unittest.main()
