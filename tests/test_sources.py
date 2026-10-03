"""抓取与采集的契约测试。

两条最要紧的：
1. 抓不到就报抓不到 —— 提取失败**不许**产出任何值。
2. 候选不等于生效 —— 抓到的值进 pending，没人工 approve 之前参数一动不动。
"""

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.sources import (
    STATUS_EXTRACT_FAILED,
    STATUS_HTTP_ERROR,
    STATUS_NEEDS_LOGIN,
    STATUS_NO_RECIPE,
    STATUS_OK,
    STATUS_URL_MISMATCH,
    Source,
    extract_value,
    fetch_source,
    find_source,
    ingest_text,
    load_sources,
    page_key,
    save_snapshot,
)
from shopee_ledger.store import Ledger
from shopee_ledger.web import _ingest_payload

REAL_PAGE = """
<html><body>
  <h1>部分站点佣金及交易手续费等费率调整通知</h1>
  <p>六、Shopee台湾站点调整：</p>
  <p>跨境直邮店铺佣金费率统一调整为14%（含税率），交易手续费率统一调整为2.5%（含税率）。</p>
  <p>七、常见问题</p>
</body></html>
"""

TAIWAN_ANCHOR = ("六、Shopee台湾站点调整：", "七、常见问题")


def inside_taiwan(phrase: str) -> str:
    """把一句话放进台湾那一段里——否则作用域锚点找不到，测试会因为"没匹配上"而假通过。"""
    return TAIWAN_ANCHOR[0] + phrase + TAIWAN_ANCHOR[1]

SHELL_PAGE = "<html><body><div id='app'></div><script src='/x.js'></script></body></html>"

# 用户实际抓到的「2026年Shopee免佣政策通知」(shopee.cn/edu/article/26619) 原文片段。
# 页面里每一处「佣金」都不是佣金率——它是免佣政策说明。曾经的宽松规则把
# 「自动免除佣金或佣金直减10%」里的 10% 抓成了佣金率，这条测试就是钉住那个 bug。
PROMO_PAGE = """
2026年Shopee免佣政策通知 2026-09-03
自2026年1月1日（北京时间）起，Shopee将对卖家在台湾站点成功开通的首个店铺，
自动免除前三个月的佣金或佣金直减10%！每月免佣订单数量上限为500单。
三、佣金激励生效时间 佣金激励生效时间以卖家首店激活销售权的时间为准。
1、我可以在哪里查询订单佣金的减免情况？ 系统将自动为您扣除相应的佣金费用。
4、佣金激励政策仅针对首开店铺的佣金进行减免，交易手续费和平台服务费等费用仍按标准收取。
"""

# shopee.cn/edu/article/26620 的真实结构：一页六个站点。
# 「14%」在这页上出现两次（新加坡的 MY-SG 项目 + 台湾的跨境直邮）。
# 下面的站点数字是页面原文；新加坡/马来两段用来验证"作用域必须锁住台湾那一段"。
MULTI_SITE_PAGE = """
Shopee部分站点佣金及交易手续费等费率调整通知 2025-12-22
自2026年1月1日起，平台将对新加坡、马来西亚、泰国、越南、菲律宾及台湾站点的佣金、交易手续费及预售商品订单服务费率进行调整。
一、Shopee新加坡站点调整：
自2026年1月1日（北京时间）起，Shopee新加坡站点跨境直邮及三方仓店铺佣金费率统一调整为16%（含税率），官方海外仓店铺佣金费率统一调整为11%（含税率），MY-SG项目（即马来西亚直送新加坡）官方海外仓店铺佣金费率统一调整为14%（含税率）。
二、Shopee马来西亚站点调整：
自2026年1月1日（北京时间）起，Shopee马来西亚站点官方海外仓店铺佣金费率统一调整为15.12%（含税率）。
六、Shopee台湾站点调整：
自2026年1月1日（北京时间）起，Shopee台湾站点免运服务（以下简称"FSS"）将并入至平台基础服务，所有店铺将免费享受FSS相关权益；跨境直邮店铺佣金费率统一调整为14%（含税率），交易手续费率统一调整为2.5%（含税率），预售商品订单服务费统一调整为3%（含税率）。
注：1.新费率仅适用于在生效日期（即北京时间2026年1月1日）后生成的订单
七、常见问题
1、佣金如何计算？ 佣金=（商品售价+Shopee提供的商品补贴-卖家优惠折扣）（不含订单运费）*佣金费率。
"""


class ExtractTest(unittest.TestCase):
    def test_regex_with_group_and_scale(self):
        rule = {"kind": "regex", "expr": "佣金[率]?[^0-9]{0,12}([0-9]+(?:\\.[0-9]+)?)\\s*%",
                "group": 1, "scale": 0.01}
        self.assertAlmostEqual(extract_value(rule, REAL_PAGE), 0.14)

    def test_thousands_separator(self):
        rule = {"kind": "regex", "expr": "門檻\\s*([0-9,]+)", "group": 1, "scale": 1}
        self.assertEqual(extract_value(rule, "門檻 2,000 元"), 2000)

    def test_no_match_returns_none_never_a_default(self):
        rule = {"kind": "regex", "expr": "佣金[^0-9]{0,12}([0-9.]+)", "group": 1, "scale": 0.01}
        self.assertIsNone(extract_value(rule, SHELL_PAGE))

    def test_json_path(self):
        rule = {"kind": "json", "expr": "data.rate"}
        self.assertEqual(extract_value(rule, '{"data": {"rate": 0.07}}'), 0.07)

    def test_manual_kind_extracts_nothing(self):
        self.assertIsNone(extract_value({"kind": "manual"}, REAL_PAGE))

    # ---- 误抓回归：推广语不是费率 --------------------------------------
    def test_commission_recipe_rejects_promo_sentence_inside_scope(self):
        """真实抓过一次：把「佣金直减10%」当成了佣金率。作用域内也必须拒掉。"""
        rule = self._rule("P-TW-COMMISSION")
        for phrase in ("自动免除前三个月的佣金或佣金直减10%！",
                       "店铺可享受佣金激励10%",
                       "佣金减免30%",
                       "三、佣金激励生效时间 佣金激励生效时间以卖家首店激活销售权的时间为准"):
            self.assertIsNone(extract_value(rule, inside_taiwan(phrase)),
                              "推广语在作用域内也不能当费率：%s" % phrase)

    def test_commission_recipe_rejects_promo_page(self):
        rule = self._rule("P-TW-COMMISSION")
        self.assertIsNone(extract_value(rule, PROMO_PAGE))

    def test_commission_recipe_still_reads_real_phrasings(self):
        rule = self._rule("P-TW-COMMISSION")
        for phrase, expected in (("跨境直邮店铺佣金费率统一调整为14%（含税率）", 0.14),
                                 ("佣金费率：2.5%", 0.025),
                                 ("佣金为 14%", 0.14),
                                 ("佣金 14%", 0.14)):
            self.assertAlmostEqual(extract_value(rule, inside_taiwan(phrase)), expected, places=9,
                                   msg=phrase)

    # ---- 作用域：一页六个站点，必须锁住台湾那一段 -----------------------
    def _rule(self, param_id: str) -> dict:
        return next(item.extract for item in load_sources() if item.param_id == param_id)

    def test_scope_picks_the_taiwan_section_not_the_first_match(self):
        """没有作用域时，正则会命中新加坡那一段（16%）——数字抓对是运气。"""
        rule = self._rule("P-TW-COMMISSION")
        naive = dict(rule)
        naive.pop("scope")
        self.assertAlmostEqual(extract_value(rule, MULTI_SITE_PAGE), 0.14, places=9)
        self.assertAlmostEqual(extract_value(naive, MULTI_SITE_PAGE), 0.16, places=9,
                               msg="去掉作用域就会抓到新加坡的 16%")

    def test_all_three_taiwan_rates_from_one_page(self):
        """真实页面原文：佣金 14% / 交易手续费 2.5% / 预售服务费 3%。"""
        self.assertAlmostEqual(extract_value(self._rule("P-TW-COMMISSION"), MULTI_SITE_PAGE), 0.14, places=9)
        self.assertAlmostEqual(extract_value(self._rule("P-TW-TXN-FEE"), MULTI_SITE_PAGE), 0.025, places=9)
        self.assertAlmostEqual(extract_value(self._rule("P-TW-PRESALE-FEE"), MULTI_SITE_PAGE), 0.03, places=9)

    def test_missing_anchor_fails_instead_of_falling_back_to_whole_page(self):
        rule = {"kind": "regex", "scope": {"after": "不存在的段落"},
                "expr": "佣金[^0-9%]{0,10}([0-9.]+)\\s*%", "group": 1, "scale": 0.01}
        self.assertIsNone(extract_value(rule, MULTI_SITE_PAGE),
                          "锚点找不到必须报失败，不能退化成全页匹配")

    def test_scope_window_limits_bleed(self):
        """before 锚点若消失，window 要兜住，别把后面几段一起圈进来。"""
        text = "甲段 " + ("填充" * 50) + " 佣金 9% 乙段 佣金 3%"
        rule = {"kind": "regex", "scope": {"after": "甲段", "window": 20},
                "expr": "佣金[^0-9%]{0,10}([0-9.]+)\\s*%", "group": 1, "scale": 0.01}
        self.assertIsNone(extract_value(rule, text), "窗口内没有数字就该失败")
        rule["scope"]["window"] = 200
        self.assertAlmostEqual(extract_value(rule, text), 0.09)


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.snaps = Path(self.tmp.name) / "snapshots"

    def tearDown(self):
        self.tmp.cleanup()

    def _source(self, **kwargs):
        base = dict(id="SRC-X", param_id="P-TW-COMMISSION",
                    url="https://example.test/a", access="public",
                    extract={"kind": "regex", "expr": "佣金[率]?[^0-9]{0,12}([0-9.]+)\\s*%",
                             "group": 1, "scale": 0.01})
        base.update(kwargs)
        return Source(**base)

    def test_fetch_ok_saves_snapshot(self):
        capture = fetch_source(self._source(), opener=lambda url: REAL_PAGE.encode(),
                               snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_OK)
        self.assertAlmostEqual(capture.value, 0.14)
        self.assertTrue(capture.snapshot_ref)
        self.assertTrue((Path(self.snaps)).exists())

    def test_js_shell_reports_extract_failed_not_a_guess(self):
        """shopee.cn/edu 就是这种：抓到了页面但内容是空的。绝不能编一个数字出来。"""
        capture = fetch_source(self._source(), opener=lambda url: SHELL_PAGE.encode(),
                               snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_EXTRACT_FAILED)
        self.assertIsNone(capture.value)
        self.assertTrue(capture.snapshot_ref, "失败也要留快照，否则没法修规则")

    def test_login_source_is_not_fetched(self):
        capture = fetch_source(self._source(access="login"), snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_NEEDS_LOGIN)
        self.assertIsNone(capture.value)

    def test_network_error_is_reported(self):
        def boom(url):
            raise urllib.error.URLError("timed out")

        capture = fetch_source(self._source(), opener=boom, snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_HTTP_ERROR)
        self.assertIsNone(capture.value)

    def test_snapshot_dedupes_by_content(self):
        first, digest_one = save_snapshot(REAL_PAGE, "SRC-X", self.snaps)
        second, digest_two = save_snapshot(REAL_PAGE, "SRC-X", self.snaps)
        self.assertEqual(digest_one, digest_two)
        self.assertEqual(first, second)
        self.assertEqual(len(list(Path(self.snaps).iterdir())), 1)


class IngestTest(unittest.TestCase):
    """浏览器送回来的渲染后文本 —— 与命令行抓取走同一份配方。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.snaps = Path(self.tmp.name) / "snapshots"
        self.sources = load_sources()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_uses_the_same_recipe(self):
        capture = ingest_text(REAL_PAGE, param_id="P-TW-COMMISSION",
                              sources=self.sources, snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_OK)
        self.assertAlmostEqual(capture.value, 0.14)
        self.assertEqual(capture.channel, "userscript")

    def test_three_recipes_sharing_one_url_do_not_collide(self):
        """一页三个费率、三条配方共用同一个 URL。按 URL 找会全部命中第一条。"""
        url = "https://shopee.cn/edu/article/26620"
        for param_id, expected in (("P-TW-COMMISSION", 0.14),
                                   ("P-TW-TXN-FEE", 0.025),
                                   ("P-TW-PRESALE-FEE", 0.03)):
            capture = ingest_text(MULTI_SITE_PAGE, param_id=param_id, url=url,
                                  sources=self.sources, snapshot_dir=self.snaps)
            self.assertEqual(capture.status, STATUS_OK, param_id)
            self.assertAlmostEqual(capture.value, expected, places=9, msg=param_id)
            self.assertEqual(capture.source_id, "SRC-" + param_id[2:], param_id)

    def test_find_source_prefers_param_id_over_url(self):
        found = find_source(self.sources, param_id="P-TW-TXN-FEE",
                            url="https://shopee.cn/edu/article/26620")
        self.assertEqual(found.id, "SRC-TW-TXN-FEE")

    def test_ingest_without_recipe_reports_it(self):
        capture = ingest_text("随便一段文本", param_id="P-NOT-EXIST",
                              sources=self.sources, snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_NO_RECIPE)
        self.assertIsNone(capture.value)

    def test_ingest_shell_text_still_fails_honestly(self):
        capture = ingest_text(SHELL_PAGE, param_id="P-TW-COMMISSION",
                              sources=self.sources, snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_EXTRACT_FAILED)
        self.assertIsNone(capture.value)

    # ---- 证据链：文本必须来自配方登记的那一页 ---------------------------
    def test_ingest_from_another_page_is_refused(self):
        """真实踩过：在 26619（免佣政策）点抓，快照却登记成 26620（费率页）。"""
        capture = ingest_text(PROMO_PAGE, param_id="P-TW-COMMISSION",
                              url="https://shopee.cn/edu/article/26619",
                              sources=self.sources, snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_URL_MISMATCH)
        self.assertIsNone(capture.value)
        self.assertEqual(capture.expected_url, "https://shopee.cn/edu/article/26620")
        self.assertEqual(capture.url, "https://shopee.cn/edu/article/26619",
                         "证据必须记实际来源，不能记配方期望的页面")
        self.assertTrue(capture.snapshot_ref, "不符也要留快照，写新配方时用得上")

    def test_ingest_from_matching_page_passes(self):
        capture = ingest_text(REAL_PAGE, param_id="P-TW-COMMISSION",
                              url="https://shopee.cn/edu/article/26620/",
                              sources=self.sources, snapshot_dir=self.snaps)
        self.assertEqual(capture.status, STATUS_OK, "结尾斜杠不该算不同页面")

    def test_page_key_normalises_host_and_slash(self):
        self.assertEqual(page_key("https://www.x.com/a/"), page_key("https://x.com/a"))
        self.assertNotEqual(page_key("https://x.com/a"), page_key("https://x.com/b"))


class CandidateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.init()
        self.snaps = Path(self.tmp.name) / "snapshots"

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _capture(self, text=REAL_PAGE):
        return ingest_text(text, param_id="P-TW-COMMISSION",
                           sources=load_sources(), snapshot_dir=self.snaps)

    def test_failed_capture_produces_no_candidate(self):
        failed = ingest_text(SHELL_PAGE, param_id="P-TW-COMMISSION",
                             sources=load_sources(), snapshot_dir=self.snaps)
        self.assertIsNone(self.ledger.record_capture(failed))
        self.assertEqual(self.ledger.pending_candidates(), [],
                         "抓失败不该产出待确认项，否则真正的变更会被淹没")

    def test_capture_does_not_touch_the_param(self):
        before = self.ledger.spec.params["P-TW-COMMISSION"].value
        candidate_id = self.ledger.record_capture(self._capture())
        self.assertIsNotNone(candidate_id)
        self.assertEqual(self.ledger.spec.params["P-TW-COMMISSION"].value, before,
                         "候选值绝不能自动生效")
        self.assertEqual(len(self.ledger.pending_candidates()), 1)

    def test_pending_marks_whether_value_changed(self):
        same = self.ledger.record_capture(self._capture())
        row = [item for item in self.ledger.pending_candidates() if item["id"] == same][0]
        self.assertFalse(row["changed"], "抓到的值与当前一致")

        changed_text = REAL_PAGE.replace("14%", "15%")
        self.ledger.record_capture(self._capture(changed_text))
        rows = self.ledger.pending_candidates()
        self.assertTrue(rows[0]["changed"], "变了的那条要排在最前")

    def test_approve_writes_override_and_unlocks(self):
        text = REAL_PAGE.replace("14%", "15%")
        candidate_id = self.ledger.record_capture(self._capture(text))
        self.ledger.approve_candidate(candidate_id, "A", note="人工确认过了")
        self.assertAlmostEqual(self.ledger.spec.params["P-TW-COMMISSION"].value, 0.15, places=6)
        self.assertEqual(self.ledger.pending_candidates(), [])
        row = self.ledger.storage.get("ParamCandidate", candidate_id)
        self.assertEqual(row["status"], "approved")

    def test_reject_leaves_param_untouched(self):
        before = self.ledger.spec.params["P-TW-COMMISSION"].value
        candidate_id = self.ledger.record_capture(self._capture(REAL_PAGE.replace("14%", "99%")))
        self.ledger.reject_candidate(candidate_id, "页面是旧版")
        self.assertEqual(self.ledger.spec.params["P-TW-COMMISSION"].value, before)
        self.assertEqual(self.ledger.storage.get("ParamCandidate", candidate_id)["status"], "rejected")

    def test_cannot_approve_twice(self):
        candidate_id = self.ledger.record_capture(self._capture())
        self.ledger.approve_candidate(candidate_id, "B")
        with self.assertRaises(ValueError):
            self.ledger.approve_candidate(candidate_id, "B")


class IngestEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.tmp.name) / "ledger.sqlite")
        # 必须注入临时目录：不然跑测试就往真实 data/snapshots 里塞固件快照
        self.snaps = Path(self.tmp.name) / "snapshots"

    def tearDown(self):
        self.tmp.cleanup()

    def test_endpoint_records_candidate_only(self):
        body = json.dumps({"param_id": "P-TW-COMMISSION", "url": "https://shopee.cn/edu/article/26620",
                           "text": REAL_PAGE, "captured_at": "2026-10-03T12:00:00+00:00"}).encode()
        result = _ingest_payload(body, self.db, snapshot_dir=self.snaps)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["value"], 0.14)
        self.assertFalse(result["changed"])
        self.assertIn("未生效", result["hint"])

    def test_endpoint_rejects_empty_text(self):
        result = _ingest_payload(json.dumps({"param_id": "P-TW-COMMISSION", "text": "  "}).encode(),
                                 self.db, snapshot_dir=self.snaps)
        self.assertFalse(result["ok"])
        self.assertIn("text 为空", result["error"])

    def test_endpoint_reports_extract_failure_with_snapshot(self):
        body = json.dumps({"param_id": "P-TW-COMMISSION",
                           "url": "https://shopee.cn/edu/article/26620",
                           "text": SHELL_PAGE}).encode()
        result = _ingest_payload(body, self.db, snapshot_dir=self.snaps)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], STATUS_EXTRACT_FAILED)
        self.assertIn("快照已存", result["hint"])
        self.assertEqual(len(list(self.snaps.iterdir())), 1, "快照要落在注入的目录里")

    def test_endpoint_surfaces_url_mismatch_with_the_expected_page(self):
        body = json.dumps({"param_id": "P-TW-COMMISSION",
                           "url": "https://shopee.cn/edu/article/26619",
                           "text": PROMO_PAGE}).encode()
        result = _ingest_payload(body, self.db, snapshot_dir=self.snaps)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], STATUS_URL_MISMATCH)
        self.assertIn("26620", result["expected_url"])
        self.assertIn("26620", result["hint"], "提示里要写明该打开哪一页")


if __name__ == "__main__":
    unittest.main()
