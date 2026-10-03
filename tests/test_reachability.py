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


class CorroborationTest(unittest.TestCase):
    """互证（cross_verified）的格式契约。

    互证比单一来源强，但只有"两个独立来源都给同一个值"才算。
    这里钉住格式，避免出现"标了 cross_verified 却只有一条来源"这种假互证。
    """

    def setUp(self):
        from shopee_ledger.spec import default_spec

        self.params = default_spec().params

    def test_every_cross_verified_param_has_an_independent_source(self):
        """互证 = 主来源（source.url）之外还有独立来源。

        不要求条数 ≥2——governance.json 只把 corroboration 定义为"快照之外的补充证据"，
        并没有规定条数。凭空加一条"必须两条"会让已有数据无端违规。
        但**至少要有一条是 exact 匹配**（有原文引用），只标 contextual 不算互证。
        """
        for param_id, param in self.params.items():
            raw = param.raw or {}
            if raw.get("verification_status") != "cross_verified":
                continue
            items = raw.get("corroboration") or []
            self.assertGreaterEqual(len(items), 1,
                                    "%s 标了 cross_verified 却没有独立来源" % param_id)
            self.assertTrue(any(item.get("match") == "exact" for item in items),
                            "%s 的互证里没有 exact 匹配，只有 contextual 不算互证" % param_id)

    def test_each_corroboration_entry_carries_a_url_and_quote(self):
        for param_id, param in self.params.items():
            if (param.raw or {}).get("verification_status") != "cross_verified":
                continue
            for item in (param.raw or {}).get("corroboration") or []:
                self.assertTrue(item.get("publisher"), param_id)
                self.assertTrue(item.get("url"), "%s 的互证缺 url" % param_id)
                self.assertTrue(item.get("quote"), "%s 的互证缺原文引用" % param_id)
                self.assertTrue(item.get("checked_at"), "%s 的互证缺查询日期" % param_id)

    def test_store_to_store_limit_is_cross_verified_and_resolves_the_conflict(self):
        """台湾店配限制：两个官方来源一致给 10kg，spec 里 10kg/5kg 的二说冲突得解。"""
        raw = self.params["P-TW-SHOPEE-SHIP-W"].raw or {}
        self.assertEqual(raw.get("verification_status"), "cross_verified")
        self.assertIn("10kg", raw.get("resolution_note", ""))
        publishers = [item.get("publisher", "") for item in raw.get("corroboration") or []]
        self.assertEqual(len(publishers), 2)
        self.assertTrue(all("Shopee 官方" in name for name in publishers),
                        "两条互证都应当是官方来源：%s" % publishers)

    def test_channel_limits_reference_file_is_present_and_consistent(self):
        """按站点的渠道限制表要与互证过的台湾值一致。"""
        import json

        path = Path(__file__).resolve().parents[1] / "spec" / "reference" / "channel-limits.json"
        self.assertTrue(path.exists(), "缺 spec/reference/channel-limits.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        tw = data["channels"]["TW"]
        self.assertEqual(tw["dims_cm"], [45, 30, 30])
        self.assertEqual(tw["max_weight_kg"], 10)
        for site in ("VN", "TH", "PH", "MY", "SG", "TW"):
            self.assertIn(site, data["channels"])

    def test_home_delivery_gap_is_recorded_with_the_attempts(self):
        """找不到的也要写成结论——把"待办"变成"已确认的缺口"，别让人重复白找。"""
        raw = self.params["P-TW-HOME-DELIV-W"].raw or {}
        self.assertTrue(raw.get("access_gate"))
        note = raw.get("access_note") or ""
        self.assertIn("没找到", note)
        self.assertIn("不可替代", note, "邻近渠道的数据要写明不能替代")


class FailureClassificationTest(unittest.TestCase):
    """失败分类：证书问题 ≠ 网络不通。

    这条区分救回过一个被搁置好几轮的来源——泰国税务那篇因为
    CERTIFICATE_VERIFY_FAILED 被当成"不可达"，其实浏览器一读就有。
    混成一类会让"本来能拿到的来源"白白搁着。
    """

    def test_certificate_error_is_its_own_kind(self):
        import ssl
        import urllib.error

        from shopee_ledger.reachability import CERT_ISSUE, classify_failure

        exc = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        self.assertEqual(classify_failure(exc), CERT_ISSUE)

    def test_plain_connection_error_is_unreachable(self):
        import urllib.error

        from shopee_ledger.reachability import UNREACHABLE, classify_failure

        self.assertEqual(classify_failure(urllib.error.URLError("timed out")), UNREACHABLE)
        self.assertEqual(classify_failure(OSError("connection refused")), UNREACHABLE)

    def test_http_error_means_the_site_is_up(self):
        import urllib.error

        from shopee_ledger.reachability import CLIENT_ERROR, OK, SERVER_ERROR, classify_failure

        # 404 说明站点答了，只是这个地址失效——和"站点挂了"不是一回事
        not_found = urllib.error.HTTPError("u", 404, "nf", {}, None)
        self.assertEqual(classify_failure(not_found), CLIENT_ERROR)
        broken = urllib.error.HTTPError("u", 500, "err", {}, None)
        self.assertEqual(classify_failure(broken), SERVER_ERROR)
        fine = urllib.error.HTTPError("u", 302, "moved", {}, None)
        self.assertEqual(classify_failure(fine), OK)

    def test_cert_issue_gets_a_hint_in_the_record(self):
        import json
        import tempfile
        from pathlib import Path as _Path

        from shopee_ledger.reachability import CERT_ISSUE, record_reachability

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            path = _Path(folder) / "r.json"
            record_reachability(results=[("https://a.test", "证书验证失败（站点可能可通，用浏览器再试）")],
                                path=path, kind_by_url={"https://a.test": CERT_ISSUE})
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data["urls"]["https://a.test"]
            self.assertEqual(entry["kind"], CERT_ISSUE)
            self.assertIn("浏览器", entry["hint"])
            self.assertFalse(entry["ok"], "证书问题不算已验证")

    def test_repo_record_flags_the_chinatax_source_as_cert_issue(self):
        """这条来源只能从浏览器读，记录里必须看得出——否则下次又会有人当它不可达。"""
        from shopee_ledger.reachability import CERT_ISSUE, load_reachability

        records = load_reachability()
        url = ("https://www.chinatax.gov.cn/chinatax/c102738/c5247291/content.html")
        entry = records.get(url)
        self.assertIsNotNone(entry, "reachability.json 里应当有这条")
        self.assertEqual(entry.get("kind"), CERT_ISSUE)


if __name__ == "__main__":
    unittest.main()
