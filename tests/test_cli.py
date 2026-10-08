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

为什么运费表要打桩
------------------
裸 `freight`（不是 `--check`）会真的联网，并且把结果写进 `spec/reference/`。
这条命令原来直接躺在下面的命令表里，于是**跑一次测试就多一份运费表快照**，
而 CostEngine 取的是"仓库里最新那份"——测试自己的副作用会改掉其他测试的前提。
实测：`python -m unittest tests.test_cli` 会写出一份
`spec/reference/sls-site-config-<今天>.json`，把写死日期的断言打成红的。

所以这里把 `fetch_site_config` 换成固定桩，并把 `REFERENCE_DIR` 指到临时目录：
保存分支照样被走到，但工作区一个字节都不动（`_run` 里那条合同由
`test_smoke_run_does_not_touch_the_repo` 守住）。桩的口径由
`tests/test_freight_by_weight.py` 对**真的**仓库快照把着。
"""

import hashlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shopee_ledger.__main__ import main  # noqa: E402

# 最小但结构真实的运费表返回。只求把"取数→扁平化→与仓库快照比对→保存"这条
# 链路走通，不求覆盖 9 个站点（那是 test_freight_by_weight 的活）。
FREIGHT_FIXTURE: dict = {
    "date": "2026-10-03",
    "site_info": [{
        "name": "TW",
        "cn_name": "台湾",
        "cargo_types": [{
            "name": "Normal",
            "cn_name": "普货",
            "channels": [{
                "name": "Shopee Xpress",
                "cn_name": "蝦皮店到店",
                "zones": [{
                    "name": "all",
                    "cn_name": "所有地区",
                    "fee_modes": [
                        {"name": "Increment", "type": "Seller", "start_weight": 0.0,
                         "end_weight": 500.0, "original_fee": 60.0,
                         "increment_unit": 500.0, "increment_amount": 35.0,
                         "effective_date": "2026-10-03"},
                        {"name": "Increment", "type": "Buyer", "start_weight": 0.0,
                         "end_weight": 500.0, "original_fee": 45.0,
                         "increment_unit": 500.0, "increment_amount": 35.0,
                         "effective_date": "2026-10-03"},
                    ],
                }],
            }],
        }],
    }],
}


def repo_fingerprint() -> dict[str, str]:
    """仓库里"数据"文件的内容指纹。用来证明测试没往工作区里写东西。"""
    out: dict[str, str] = {}
    for pattern in ("spec/**/*.json", "spec/reference/*.json", "spec/*.json"):
        for path in ROOT.glob(pattern):
            if path.is_file():
                out[str(path.relative_to(ROOT))] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
    return out


# (参数, 说明)。都只碰本地：不联网、不开浏览器、不写 spec/。
# 例外只有 `freight`：它本来就会联网+写 spec/，由 _run 单独打桩兜住。
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
        # 裸 freight 会往 REFERENCE_DIR 里写文件——指到临时目录，别脏了工作区。
        # 用 import_module 而不是 from ... import：多 import 一次不算错，少一次才要命。
        freight = importlib.import_module("shopee_ledger.freight")
        self._ref_patch = mock.patch.object(
            freight, "REFERENCE_DIR", Path(self.tmp.name) / "reference")
        self._ref_patch.start()
        self.addCleanup(self._ref_patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _freight_config(date: str) -> dict:
        config = json.loads(json.dumps(FREIGHT_FIXTURE))   # 每个用例一份，别共用可变对象
        config["date"] = date
        return config

    def _run(self, args: list[str]) -> tuple[int, str]:
        """跑一条命令，返回 (退出码, 输出)。未处理异常直接让测试失败。"""
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        # bare freight 是唯一会联网+写盘的本地命令，这里给它一个固定桩。
        # 桩的日期沿用仓库里已入库的那份（2026-10-03），这样"与仓库快照比对"
        # 有确定的基准，断言也不必跟着系统日期漂。
        patcher = (mock.patch("shopee_ledger.freight.fetch_site_config",
                              side_effect=self._freight_config)
                   if args and args[0] == "freight" else None)
        if patcher is not None:
            patcher.start()
        try:
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(["--db", self.db] + args)
            except SystemExit as exc:          # argparse 的正常退出
                code = int(exc.code or 0)
            except Exception as exc:           # 未处理异常 = 测试失败
                self.fail("`%s` 抛了未处理异常 %s: %s\n%s"
                          % (" ".join(args), type(exc).__name__, exc, stderr.getvalue()[-500:]))
        finally:
            if patcher is not None:
                patcher.stop()
        return code, stdout.getvalue() + stderr.getvalue()

    def test_smoke_run_does_not_touch_the_repo(self):
        """合同：跑这些本地命令不许改动仓库里的数据文件。

        这条守的是真实踩过的坑——裸 freight 会真的联网并把运费表写进
        spec/reference/。一旦某条命令又偷偷写盘，同时写坏别处的前提，
        这里会直接报出是哪个文件变了，而不是让下游某个写死日期的断言
        在几天后莫名其妙变红。
        """
        before = repo_fingerprint()
        self._run(["init"])
        for args, _label in LOCAL_COMMANDS:
            if args != ["init"]:
                self._run(args)
        after = repo_fingerprint()

        changed = sorted(
            "%s：%s → %s" % (
                name,
                "不存在" if name not in before else before[name][:12],
                "已删除" if name not in after else after[name][:12])
            for name in set(before) | set(after) if before.get(name) != after.get(name))
        self.assertEqual(changed, [], "冒烟测试改动了仓库文件（不该写盘）：%s" % changed)

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
