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


class AccessGateTest(unittest.TestCase):
    """参数的「公开渠道不可得」结论也要有契约——否则后来人会重复白找。

    这条规则来自实测：SLS 完整费率表在企业微信里、入驻正文要 Shopee 账号登录。
    把"待办"变成"已确认的门槛"，是这一轮最省时间的产出。
    """

    KNOWN_GATES = {"wecom_login", "shopee_account_login", "seller_center_login", "manual_only"}

    def setUp(self):
        from shopee_ledger.spec import default_spec

        self.params = default_spec().params

    def test_gate_values_come_from_a_known_set(self):
        wrong = []
        for param_id, param in self.params.items():
            gate = (param.raw or {}).get("access_gate")
            if gate and gate not in self.KNOWN_GATES:
                wrong.append((param_id, gate))
        self.assertEqual(wrong, [], "access_gate 只能是已知取值，避免各写各的")

    def test_every_gate_explains_itself(self):
        for param_id, param in self.params.items():
            raw = param.raw or {}
            if raw.get("access_gate"):
                self.assertTrue(raw.get("access_note"),
                                "%s 有 access_gate 就必须有 access_note" % param_id)
                self.assertTrue(raw.get("accessible_via"),
                                "%s 要写清从哪里能拿到" % param_id)

    def test_the_measured_gates_are_recorded(self):
        """实测确认的门槛，掉了就说明有人把它们删了。

        P-TW-SLS-TIERS **不在**此列：它一度被误判为 wecom_login，
        后来发现定价模拟器有公开接口（9 站点完整运费表），已纠正。
        这张清单只留真正验过的。
        """
        expected = {"P-ONB-ENTRY": "shopee_account_login",
                    "P-ONB-FIRST-SITE": "shopee_account_login",
                    "P-ONB-FLOW-REQ": "shopee_account_login",
                    "P-TW-FX": "seller_center_login"}
        for param_id, gate in expected.items():
            raw = (self.params[param_id].raw or {})
            self.assertEqual(raw.get("access_gate"), gate, param_id)

    def test_freight_table_is_not_falsely_marked_as_gated(self):
        """被证伪的判断不能留在文件里——运费表可从定价模拟器公开接口取。"""
        raw = self.params["P-TW-SLS-TIERS"].raw or {}
        self.assertNotIn("access_gate", raw, "SLS 运费表可从公开接口取，不该标为需登录")
        self.assertEqual((raw.get("source") or {}).get("kind"), "official_api")
        self.assertIn("pricing-simulator", (raw.get("source") or {}).get("api", ""))


if __name__ == "__main__":
    unittest.main()
