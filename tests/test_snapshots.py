"""快照体检：**引用字符串在 ≠ 证据在**。

这组测试守着一个真实漏洞：.gitignore 曾经忽略整个 data/，于是 32 个 A 级参数里
有 22 个的 snapshot_ref 指向一个没进版本库的文件。INV-003 只查"引用字符串在不在"，
所以一路绿灯——但 clone 出来证据链是断的。
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.snapshots import (  # noqa: E402
    audit,
    check,
    is_tracked,
    resolve,
)
from shopee_ledger.store import Ledger  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class ResolveTest(unittest.TestCase):
    def test_relative_path_resolves_against_the_repo(self):
        path = resolve("data/snapshots/x.txt")
        self.assertTrue(str(path).endswith("data\\snapshots\\x.txt")
                        or str(path).endswith("data/snapshots/x.txt"))

    def test_backslashes_are_normalised(self):
        """verified.json 里存的是 Windows 反斜杠——换平台不能就找不到文件。"""
        self.assertEqual(resolve("data\\snapshots\\a.txt"), resolve("data/snapshots/a.txt"))

    def test_none_gives_none(self):
        self.assertIsNone(resolve(None))
        self.assertIsNone(resolve(""))


class IsTrackedTest(unittest.TestCase):
    def setUp(self):
        probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                               cwd=ROOT, capture_output=True, text=True)
        if probe.returncode != 0:
            self.skipTest("不在 git 仓库里")

    def test_a_tracked_spec_file_is_tracked(self):
        self.assertIs(is_tracked(ROOT / "spec" / "governance.json"), True)

    def test_an_ignored_file_is_not_tracked(self):
        """data/*.log 是忽略的——这正是当初快照的处境。"""
        target = ROOT / "data" / "probe-ignored.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        created = not target.exists()
        target.write_text("x", encoding="utf-8")
        try:
            self.assertIs(is_tracked(target), False)
        finally:
            if created:
                target.unlink()

    def test_a_path_outside_the_repo_cannot_be_judged(self):
        """判不出来要回 None，**不能当成"没进"**——否则没 git 的环境里全是假警报。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            outside = Path(folder) / "x.txt"
            outside.write_text("x", encoding="utf-8")
            self.assertIsNone(is_tracked(outside, ROOT))


class CheckTest(unittest.TestCase):
    def test_missing_ref(self):
        health = check(None)
        self.assertFalse(health.exists)
        self.assertFalse(health.usable)
        self.assertIn("没有 snapshot_ref", health.detail)

    def test_missing_file(self):
        health = check("data/snapshots/definitely-not-here.txt")
        self.assertFalse(health.exists)
        self.assertFalse(health.usable)

    def test_a_real_tracked_snapshot_is_usable(self):
        health = check("spec/reference/sls-site-config-2026-10-03.json")
        self.assertTrue(health.exists)
        self.assertIs(health.tracked, True)
        self.assertTrue(health.usable)


class RepoEvidenceTest(unittest.TestCase):
    """仓库级的体检：每个 A 级参数的快照都得能用。

    注意用 **Ledger 的合并视图**（spec 文件 + verified.json 覆盖层）而不是 Spec.load：
    很多 A 级参数的快照是抓取时记进覆盖层的，只看 spec 文件会误判成"没有快照"
    ——这正是第一版这条测试踩的坑。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "l.sqlite")   # verified_path 默认即合并
        self.ledger.init()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def _a_level(self):
        return [p for p in self.ledger.spec.params.values() if p.evidence_level == "A"]

    def test_every_a_level_snapshot_is_present_and_versioned(self):
        broken = [item for item in audit(self.ledger.spec.params)
                  if not item.usable and item.ref]
        self.assertEqual(broken, [],
                         "这些 A 级参数的快照不能用（没进版本库或文件不在）：%s"
                         % [(item.ref, item.detail) for item in broken])

    def test_no_a_level_param_lacks_a_snapshot(self):
        """A 级参数的快照引用必须齐全。

        这条原来是「没写 snapshot_ref 的应当只剩 help.shopee.tw 那几个」——
        那 4 个（EZWAY / FREQ-IMPORT / PRESALE-TAX-COLLECT / TAX-THRESHOLD）
        来源打不开又没快照，却挂着 A，属于**高估**。已按 INV-003 降为 B。
        于是这条测试的前提消失了，改成更强的断言：A 级一个都不许缺。
        """
        missing = [p.id for p in self._a_level() if not (p.source or {}).get("snapshot_ref")]
        self.assertEqual(missing, [],
                         "这些 A 级参数没有快照引用——要么补凭据，要么降级：%s" % missing)

    def test_the_demoted_ones_are_now_b_level_with_a_reason(self):
        """降级要留下理由，不能只是把字母改掉。"""
        for param_id in ("P-TW-EZWAY", "P-TW-FREQ-IMPORT",
                         "P-TW-PRESALE-TAX-COLLECT", "P-TW-TAX-THRESHOLD"):
            param = self.ledger.spec.params.get(param_id)
            self.assertIsNotNone(param, param_id)
            self.assertEqual(param.evidence_level, "B", param_id)
            self.assertIn("help.shopee.tw", (param.source or {}).get("url", ""),
                          "%s 的来源应当仍指向那个打不开的地址（缺口要留痕）" % param_id)

    def test_most_a_level_params_carry_a_usable_snapshot(self):
        items = [item for item in audit(self.ledger.spec.params) if item.ref]
        usable = [item for item in items if item.usable]
        self.assertEqual(len(usable), len(items))
        self.assertGreaterEqual(len(usable), 20)

    def test_snapshots_directory_is_not_ignored(self):
        """data/snapshots 不该再出现在 .gitignore 里——取消注释就会让 22 条证据再次脱库。"""
        lines = [line.strip() for line in (ROOT / ".gitignore").read_text(
            encoding="utf-8").splitlines()]
        active = [line for line in lines if line and not line.startswith("#")]
        self.assertNotIn("data/snapshots/", active, "快照是原始证据，不能忽略")


if __name__ == "__main__":
    unittest.main()
