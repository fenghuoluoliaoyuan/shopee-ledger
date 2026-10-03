"""架构适应度测试：把「真值分两层」这条规矩变成机器可查的规则。

为什么需要（这类 bug 我犯过四次）
--------------------------------
真值现在分两层：``spec/`` 文件是制度性配置，``spec/verified.json`` 是随 git 走的核实成果。
``default_spec()`` 只返回**基础层**。任何拿它来判门禁/算钱的地方，看到的都是升级前的等级：
已核实的参数显示「未核实」、本该硬拦的商品被判成 WARN 放行。

修过四次之后我决定不再靠记性：用一条**适应度函数**（借鉴 ArchUnit / import-linter 的做法）
把调用点钉成允许清单。新增一处就会失败，逼人写清楚"为什么这里可以用基础层"。

判定标准很简单：**这处代码会不会读参数的证据等级或值？**
会 → 必须用 Ledger.spec（含覆盖层）。不会（只读 registry/DDL/状态机元数据）→ 允许。
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shopee_ledger"

# (文件, 所在函数) → 为什么这里可以用基础层。新增必须在此登记。
ALLOWED = {
    ("store.py", "spec"): "这里正是构建核实后视图的地方（基础层 + verified.json + 库覆盖）",
    ("store.py", "_seed_tasks"): "任务定义是元数据，与证据等级无关",
    ("storage.py", "__init__"): "DDL 与字段类型来自 entities 元数据，与证据等级无关",
    ("fulfillment.py", "_definition"): "订单状态机定义是元数据",
    ("fulfillment.py", "<module>"): "模块级默认状态机，元数据",
    ("statemachine.py", "machine_for"): "状态机缺省参数，元数据",
    ("web.py", "today"): "只读 registry 的市场清单",
    ("web.py", "params_page"): "只读 registry 的市场清单",
    ("web.py", "spec_page"): "只用于 Storage 的 DDL 与指纹；参数表已改用 ledger.spec",
    ("web.py", "products_page"): "只读 registry 的市场清单",
    ("__main__.py", "_run"): "spec-info 的指纹记录制度性配置版本，与核实覆盖无关",
    # 以下四个是**危险类**：函数签名允许 spec=None 时静默回落。调用方必须传 ledger.spec，
    # 已由本文件的行为测试守着（见 GateEntryPointTest）。之所以留在这里，是因为测试与
    # 适配器需要"显式不传"这个能力来表达"我要基础层"。
    ("desk.py", "build_context"): "适配器缺省值；调用方（web/_gate_kwargs）必须传 ledger.spec",
    ("desk.py", "listing_gate_result"): "适配器缺省值；同上",
    ("desk.py", "survival_advice"): "适配器缺省值；web 已传 ledger.spec",
    ("veto.py", "evaluate_flags"): "适配器缺省值；store.py 调用时传 self.spec",
}

# 监视**整个包**：任何模块里新增的 default_spec() 调用都必须登记。
# 只监视"看起来危险"的模块不够——危险与否正是这个测试要替人判断的事。
WATCHED = tuple(sorted(path.name for path in PACKAGE.glob("*.py")))


def call_sites(path: Path) -> list[tuple[str, int]]:
    """返回文件里所有 default_spec() 调用点的 (所在函数, 行号)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(id(child), node)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "default_spec":
            continue
        enclosing = owner.get(id(node))
        found.append((enclosing.name if enclosing else "<module>", node.lineno))
    return found


class BaseSpecFitnessTest(unittest.TestCase):
    def test_every_call_site_is_registered(self):
        """未登记的 default_spec() 调用一律失败——逼人说明为什么可以用基础层。"""
        unregistered = []
        for name in WATCHED:
            path = PACKAGE / name
            if not path.exists():
                continue
            for function, line in call_sites(path):
                if (name, function) not in ALLOWED:
                    unregistered.append("%s:%d 在 %s()" % (name, line, function))
        self.assertEqual(
            unregistered, [],
            "新增了 default_spec() 调用且未登记。先问：这里会读参数的证据等级或值吗？\n"
            "  会  → 改用 ledger.spec（含 spec/verified.json 覆盖层）\n"
            "  不会 → 在 tests/test_architecture.py 的 ALLOWED 里登记并写明理由\n"
            + "\n".join("  · " + item for item in unregistered))

    def test_allowlist_has_no_stale_entries(self):
        """登记了但代码里已经没有了 —— 说明清单该更新，否则会掩盖真实调用点。"""
        present = set()
        for name in WATCHED:
            path = PACKAGE / name
            if not path.exists():
                continue
            for function, _line in call_sites(path):
                present.add((name, function))
        stale = sorted(set(ALLOWED) - present)
        self.assertEqual(stale, [], "允许清单里有已经不存在的位置，请清理")

    def test_every_allowlist_entry_has_a_reason(self):
        for key, reason in ALLOWED.items():
            self.assertTrue(reason and len(reason) > 8, "%s 的理由太短：%r" % (key, reason))


class GateEntryPointTest(unittest.TestCase):
    """门禁入口必须收到**核实后的视图**，否则会把该拦的商品放行。

    实测后果：P-SLS-BANNED 基础是 E 级（降级成 WARN）、核实后是 A 级（硬拦）。
    少传一个 spec，网页就把"需人工确认"显示成"可上架"。
    """

    def setUp(self):
        import tempfile

        from shopee_ledger.store import Ledger

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 这个测试要的正是**仓库里那份核实成果**，所以用默认 verified_path。
        self.ledger = Ledger(Path(self.tmp.name) / "l.sqlite")
        self.ledger.init()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_gate_kwargs_carries_the_verified_spec(self):
        import shopee_ledger.web as web

        row = {"id": 1, "sample_bought": 1, "weight_g": 100, "weight_is_measured": 1,
               "purchase_cny": 20.0, "photo_ready": 1, "title_text": "t", "detail_ready": 1}
        kwargs = web._gate_kwargs(self.ledger, row)
        self.assertIn("spec", kwargs, "_gate_kwargs 必须带上 spec，否则门禁回落到基础 spec")
        self.assertIs(kwargs["spec"], self.ledger.spec)

    def test_verified_spec_changes_the_verdict(self):
        """核心断言：同一个候选品，用核实后的视图判定结果必须不同。

        这不是"实现细节"——它正是这个 bug 的用户可见后果。
        """
        from shopee_ledger.desk import listing_gate

        ctx = dict(supplier_count=3, sample_bought=True, weighed=True, measured=True,
                   purchase_price_cny=20.0, photo_ready=True, title_ready=True, detail_ready=True)
        base = listing_gate(None, market="TW", **ctx)
        verified = listing_gate(None, market="TW", spec=self.ledger.spec, **ctx)
        self.assertNotEqual(
            base, verified,
            "仓库里的核实成果应当改变门禁结论；若两者相同，说明覆盖层没接进门禁")

    def test_survival_advice_receives_the_verified_spec(self):
        import inspect

        import shopee_ledger.web as web

        source = inspect.getsource(web)
        self.assertIn("survival_advice(rate, ledger.spec)", source,
                      "survival_advice 读 P-KPI-SURVIVAL-TH 的值，必须传核实后的 spec")


if __name__ == "__main__":
    unittest.main()
