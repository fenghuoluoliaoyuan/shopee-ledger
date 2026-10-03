"""列表页监控的契约测试。

核心主张：**列表页只做发现，不产出任何参数值。**
"新文档出现了"是有用信号（费率变更通常新发一篇），但值必须另抓、另确认。
"""

import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http.server import ThreadingHTTPServer  # noqa: E402

from shopee_ledger.sources import ingest_text, load_sources  # noqa: E402
from shopee_ledger.store import Ledger  # noqa: E402
from shopee_ledger.watch import (  # noqa: E402
    diff_entries,
    entries_from_links,
    load_watches,
    normalize_date,
    parse_api_calls,
    parse_listing_html,
)
from shopee_ledger.web import Handler, _ingest_payload  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_sources import REAL_PAGE  # noqa: E402

# 依据用户实测的「政策与物流」列表页（标题 + 日期）
LISTING_LINKS = [
    {"href": "https://shopee.cn/edu/article/30001", "text": "Shopee泰国站点优选/优选+卖家标准更新通知",
     "date": "Shopee泰国站点优选/优选+卖家标准更新通知\n2026-09-30"},
    {"href": "https://shopee.cn/edu/article/30002", "text": "Shopee越南站点专项买家分期付款计划上线及费率优惠通知",
     "date": "…\n2026-09-30"},
    {"href": "/edu/article/30003", "text": "Shopee官方海外仓官方头程服务十月渠道费率通知",
     "date": "…\n2026-09-23"},
    {"href": "https://shopee.cn/edu/article/26619", "text": "2026年Shopee免佣政策通知",
     "date": "…\n2026-09-03"},
]


class ListingParseTest(unittest.TestCase):
    def test_parses_entries_and_dates(self):
        entries = entries_from_links(LISTING_LINKS, base_url="https://shopee.cn/edu/category/policy")
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0].article_id, "30001")
        self.assertEqual(entries[0].published_at, "2026-09-30")
        self.assertEqual(entries[2].url, "https://shopee.cn/edu/article/30003",
                         "相对链接要补成绝对地址")

    def test_dedupes_repeated_links(self):
        doubled = LISTING_LINKS + LISTING_LINKS
        self.assertEqual(len(entries_from_links(doubled)), 4)

    def test_ignores_non_article_links(self):
        links = LISTING_LINKS + [
            {"href": "https://shopee.cn/edu/course/999", "text": "课程"},
            {"href": "https://seller.shopee.cn/portal", "text": "卖家中心"},
            {"href": "https://shopee.cn/edu/article/30004", "text": ""},
        ]
        ids = [entry.article_id for entry in entries_from_links(links)]
        self.assertEqual(ids, ["30001", "30002", "30003", "26619"])

    def test_ignores_nav_links_that_point_at_articles(self):
        """实测踩过：页脚「iOS版」也指向 /article/，混进了清单。"""
        links = LISTING_LINKS + [
            {"href": "https://shopee.cn/edu/article/4579", "text": "iOS版", "date": ""},
            {"href": "https://shopee.cn/edu/article/4580", "text": "更多", "date": ""},
        ]
        ids = [entry.article_id for entry in entries_from_links(links)]
        self.assertEqual(ids, ["30001", "30002", "30003", "26619"])

    def test_date_normalisation(self):
        self.assertEqual(normalize_date("发布于 2026/9/3"), "2026-09-03")
        self.assertEqual(normalize_date("2026.9.30"), "2026-09-30")
        self.assertEqual(normalize_date("没有日期"), "")

    def test_watch_config_is_declared(self):
        self.assertTrue(load_watches(), "sources.json 里应当有 watch 段")


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.previous = {entry.article_id: entry.as_row()
                         for entry in entries_from_links(LISTING_LINKS)}

    def test_new_articles_are_detected(self):
        current = entries_from_links(LISTING_LINKS + [
            {"href": "https://shopee.cn/edu/article/30999", "text": "台湾站点佣金费率调整通知",
             "date": "2026-10-01"}])
        diff = diff_entries(self.previous, current)
        self.assertEqual([entry.article_id for entry in diff["new"]], ["30999"])
        self.assertEqual(diff["total"], 5)

    def test_title_change_is_detected(self):
        changed = [dict(LISTING_LINKS[0], text="Shopee泰国站点优选标准更新通知（修订）")] + LISTING_LINKS[1:]
        diff = diff_entries(self.previous, entries_from_links(changed))
        self.assertEqual([entry.article_id for entry in diff["changed"]], ["30001"])
        self.assertEqual(diff["new"], [])

    def test_disappearing_articles_are_reported(self):
        diff = diff_entries(self.previous, entries_from_links(LISTING_LINKS[:2]))
        self.assertEqual(sorted(diff["gone"]), ["26619", "30003"])


class RecordListingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.init()

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_first_crawl_marks_everything_new(self):
        result = self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS))
        self.assertEqual(result["total"], 4)
        self.assertEqual(len(result["new"]), 4)
        self.assertEqual(len(self.ledger.watch_entries(only_new=True)), 4)

    def test_second_crawl_finds_nothing_new(self):
        self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS))
        again = self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS))
        self.assertEqual(again["new"], [], "同一份列表再抓不该报新文档")
        self.assertEqual(len(self.ledger.watch_entries()), 4, "也不该重复插入")

    def test_third_crawl_reports_only_the_addition(self):
        self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS))
        with_new = entries_from_links(LISTING_LINKS + [
            {"href": "https://shopee.cn/edu/article/30999", "text": "台湾站点佣金费率调整通知",
             "date": "2026-10-01"}])
        result = self.ledger.record_listing("WATCH-TEST", with_new)
        self.assertEqual([item["article_id"] for item in result["new"]], ["30999"],
                         "diff 只该报真正新出现的那篇")
        self.assertEqual(result["total"], 5)

    def test_unread_stays_unread_until_marked(self):
        """未读就是未读——抓十次也不会自己变成已读，否则你会漏掉它。"""
        self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS))
        self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS))
        self.assertEqual(len(self.ledger.watch_entries(only_new=True)), 4)
        target = self.ledger.watch_entries(only_new=True)[0]
        self.ledger.mark_watch_seen(target["id"])
        self.assertEqual(len(self.ledger.watch_entries(only_new=True)), 3)

    def test_entries_sorted_by_date_desc(self):
        self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS))
        dates = [row["published_at"] for row in self.ledger.watch_entries()]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_listing_records_no_param_value(self):
        """列表页绝不能产出参数值——它没有正文。"""
        before = self.ledger.spec.params["P-TW-COMMISSION"].value
        self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS))
        self.assertEqual(self.ledger.spec.params["P-TW-COMMISSION"].value, before)
        self.assertEqual(self.ledger.pending_candidates(), [])

    def test_audit_trail_records_the_crawl(self):
        self.ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS),
                                   page_url="https://shopee.cn/edu/category/policy")
        actions = [row["action"] for row in self.ledger.storage.list("AuditLog", limit=50)]
        self.assertIn("watch.listing", actions)


class IngestListingEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.tmp.name) / "ledger.sqlite")
        self.snaps = Path(self.tmp.name) / "snapshots"

    def tearDown(self):
        self.tmp.cleanup()

    def test_links_payload_is_treated_as_listing(self):
        body = json.dumps({"url": "https://shopee.cn/edu/category/policy",
                           "links": LISTING_LINKS}).encode()
        result = _ingest_payload(body, self.db, snapshot_dir=self.snaps)
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 4)
        self.assertEqual(len(result["new"]), 4)
        self.assertIn("只做发现", result["hint"])

    def test_listing_url_resolves_to_the_configured_watch(self):
        """用户给的列表页地址要能对上 watch 配置，否则记录会散在 WATCH-MANUAL 下。"""
        watches = load_watches()
        target = [watch for watch in watches if watch.id == "WATCH-EDU-POLICY"][0]
        self.assertTrue(target.url.startswith("https://"), "watch URL 不能还是占位文字")
        self.assertIn("sub_cat_id", target.url, "带查询串的列表页必须整串保留")

    def test_ingest_uses_watch_id_matched_by_url(self):
        target = [watch for watch in load_watches() if watch.id == "WATCH-EDU-POLICY"][0]
        body = json.dumps({"url": target.url, "links": LISTING_LINKS}).encode()
        result = _ingest_payload(body, self.db, snapshot_dir=self.snaps)
        self.assertTrue(result["ok"])
        self.assertEqual(result["watch_id"], "WATCH-EDU-POLICY")
        self.assertTrue(result["known_watch"])

    def test_unknown_listing_url_falls_back_to_manual_watch(self):
        body = json.dumps({"url": "https://shopee.cn/edu/category?sub_cat_id=9999",
                           "links": LISTING_LINKS}).encode()
        result = _ingest_payload(body, self.db, snapshot_dir=self.snaps)
        self.assertEqual(result["watch_id"], "WATCH-MANUAL")
        self.assertFalse(result["known_watch"])

    def test_links_payload_without_articles_is_refused(self):
        body = json.dumps({"url": "https://x.test",
                           "links": [{"href": "/course/1", "text": "课"}]}).encode()
        result = _ingest_payload(body, self.db, snapshot_dir=self.snaps)
        self.assertFalse(result["ok"])
        self.assertIn("没解析出任何条目", result["error"])


class WatchHttpTest(unittest.TestCase):
    """走真实 HTTP 路由：油猴脚本打的就是这两个地址。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        Handler.db_path = str(Path(self.tmp.name) / "ledger.sqlite")
        # 必须注入：不然跑测试就往真实 data/snapshots 里写固件快照
        Handler.snapshot_dir = str(Path(self.tmp.name) / "snapshots")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        Handler.snapshot_dir = None
        self.tmp.cleanup()

    def test_serves_the_userscript_for_auto_update(self):
        """脚本由本机托管，Tampermonkey 按 @updateURL 自动更新——不用再手工粘贴。"""
        body = urlopen(self.base + "/shopee-capture.user.js", timeout=10).read().decode("utf-8")
        self.assertIn("@updateURL", body)
        self.assertIn("http://127.0.0.1:8765/shopee-capture.user.js", body)
        self.assertIn("GM_xmlhttpRequest", body)

    def test_watches_endpoint_lists_the_configured_listing(self):
        data = json.loads(urlopen(self.base + "/watches.json", timeout=10).read().decode())
        self.assertEqual(data[0]["id"], "WATCH-EDU-POLICY")
        self.assertIn("sub_cat_id", data[0]["url"], "查询串要整串给出，脚本据此比对当前页")

    def test_ingest_listing_over_http(self):
        body = json.dumps({"url": "https://shopee.cn/edu/category?sub_cat_id=1066",
                           "links": LISTING_LINKS}).encode()
        response = urlopen(Request(self.base + "/ingest", data=body,
                                   headers={"Content-Type": "application/json"}), timeout=10)
        result = json.loads(response.read().decode())
        self.assertTrue(result["ok"])
        self.assertEqual(result["watch_id"], "WATCH-EDU-POLICY")
        self.assertEqual(len(result["new"]), 4)

    def test_ingest_text_still_works_over_http(self):
        payload = {"param_id": "P-TW-COMMISSION",
                   "url": "https://shopee.cn/edu/article/26620", "text": REAL_PAGE}
        response = urlopen(Request(self.base + "/ingest", data=json.dumps(payload).encode(),
                                   headers={"Content-Type": "application/json"}), timeout=10)
        result = json.loads(response.read().decode())
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["value"], 0.14)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "listing-page.html"


class ListingHtmlParserTest(unittest.TestCase):
    """对用户保存的**真实页面**写解析器——不再猜选择器。

    固件来自 https://shopee.cn/edu/category?sub_cat_id=1066 的「网页另存为」结果：
    Chrome 存的是渲染后的 DOM，所以里面有真实链接与分页控件。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")

    def test_parses_every_item(self):
        entries, _ = parse_listing_html(self.html,
                                        base_url="https://shopee.cn/edu/category?sub_cat_id=1066")
        self.assertEqual(len(entries), 15)
        self.assertEqual(entries[0].article_id, "28402")
        self.assertEqual(entries[0].published_at, "2026-09-30")
        self.assertIn("泰国站点优选", entries[0].title)

    def test_title_and_date_are_paired_per_item(self):
        """日期在 <a> 外面（article-title-bottom > bottom-time），最容易串行。"""
        entries, _ = parse_listing_html(self.html)
        pairs = {entry.article_id: (entry.published_at, entry.title) for entry in entries}
        self.assertEqual(pairs["28203"][0], "2026-09-04")
        self.assertIn("买家自提渠道重量限制", pairs["28203"][1])
        self.assertEqual(pairs["26619"][0], "2026-09-03")
        self.assertIn("免佣政策", pairs["26619"][1])
        self.assertEqual(pairs["27730"][0], "2026-08-03")

    def test_reads_the_pager(self):
        _, pager = parse_listing_html(self.html)
        self.assertEqual(pager["current"], "1")
        self.assertTrue(pager["has_next"], "第 1 页的 next 按钮应当可用")
        self.assertIn("9", pager["pages"])
        self.assertNotIn("...", pager["pages"], "省略号不算页码")

    def test_unrelated_html_yields_nothing(self):
        entries, pager = parse_listing_html("<html><body>这里没有列表</body></html>")
        self.assertEqual(entries, [])
        self.assertIsNone(pager["current"])
        self.assertFalse(pager["has_next"])

    def test_endpoint_accepts_rendered_html(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = str(Path(tmp.name) / "l.sqlite")
        body = json.dumps({"url": "https://shopee.cn/edu/category?sub_cat_id=1066",
                           "html": self.html}).encode()
        result = _ingest_payload(body, db, snapshot_dir=Path(tmp.name) / "s")
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 15)
        self.assertEqual(result["watch_id"], "WATCH-EDU-POLICY")
        self.assertEqual(result["pager"]["current"], "1")
        tmp.cleanup()


class ApiDiscoveryTest(unittest.TestCase):
    """列表页是 SPA，接口藏在混淆 JS 里。让浏览器把真实请求报回来。"""

    CALLS = [
        "https://shopee.cn/track/report",
        "https://c-api-bit.shopeemobile.com/shop/edu/v2/notice_page",
        "https://deo.shopeesz.com/shopee/edu/app.js",
        "https://shopee.cn/edu/api/v1/notice/list",
    ]

    def test_keeps_every_http_request_not_just_likely_ones(self):
        """上一版按关键词筛选，把浏览器报回来的 3 个请求全丢了——过滤条件本身是猜测。"""
        kept = parse_api_calls(self.CALLS)
        self.assertEqual(len(kept), 4, "http(s) 请求一律保留，不做关键词过滤")
        self.assertIn("https://c-api-bit.shopeemobile.com/shop/edu/v2/notice_page", kept)

    def test_likely_endpoints_sort_first(self):
        kept = parse_api_calls(self.CALLS)
        self.assertIn("/api/", kept[0], "像接口的排前面，方便先看")

    def test_ignores_non_http_and_duplicates(self):
        kept = parse_api_calls(["javascript:void(0)", "", None] + self.CALLS + self.CALLS)
        self.assertEqual(len(kept), len(set(kept)))
        self.assertEqual(len(kept), 4)

    def test_records_and_surfaces_discovered_apis(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        ledger = Ledger(Path(tmp.name) / "l.sqlite")
        ledger.init()
        try:
            ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS),
                                  api_calls=self.CALLS)
            ledger.record_listing("WATCH-TEST", entries_from_links(LISTING_LINKS),
                                  api_calls=self.CALLS)
            apis = ledger.discovered_apis()
            urls = [item["url"] for item in apis]
            self.assertIn("https://c-api-bit.shopeemobile.com/shop/edu/v2/notice_page", urls)
            self.assertEqual(max(item["hits"] for item in apis), 2, "同一接口出现两次要累计")
        finally:
            ledger.close()
            tmp.cleanup()

    def test_endpoint_returns_discovered_apis(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = str(Path(tmp.name) / "l.sqlite")
        body = json.dumps({"url": "https://shopee.cn/edu/category?sub_cat_id=1066",
                           "links": LISTING_LINKS, "api_calls": self.CALLS}).encode()
        result = _ingest_payload(body, db, snapshot_dir=Path(tmp.name) / "s")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["api_calls"]), 4)
        tmp.cleanup()


class ConflictTest(unittest.TestCase):
    """多篇文档写了不同的值：系统排序、标最新，**绝不替人选**。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.init()
        self.snaps = Path(self.tmp.name) / "snapshots"
        self.url = "https://shopee.cn/edu/article/26620"

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _capture(self, percent: str, captured_at: str) -> int:
        text = REAL_PAGE.replace("14%", percent + "%").replace("2.5%", "9.9%") \
            if "14%" in REAL_PAGE else REAL_PAGE
        capture = ingest_text(text, param_id="P-TW-COMMISSION", url=self.url,
                              captured_at=captured_at, sources=load_sources(),
                              snapshot_dir=self.snaps)
        return self.ledger.record_capture(capture)

    def test_two_documents_produce_a_conflict(self):
        self._capture("15", "2026-10-01T00:00:00+00:00")
        self._capture("16", "2026-11-01T00:00:00+00:00")
        conflicts = self.ledger.candidate_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["param_id"], "P-TW-COMMISSION")
        self.assertEqual(len(conflicts[0]["candidates"]), 2)

    def test_newest_is_marked_not_chosen(self):
        old = self._capture("15", "2026-10-01T00:00:00+00:00")
        new = self._capture("16", "2026-11-01T00:00:00+00:00")
        rows = {row["id"]: row for row in self.ledger.pending_candidates()}
        self.assertFalse(rows[old]["is_newest"])
        self.assertEqual(rows[old]["superseded_by"], new)
        self.assertTrue(rows[new]["is_newest"])
        self.assertIsNone(rows[new]["superseded_by"])

    def test_conflicting_param_sorts_first(self):
        self._capture("15", "2026-10-01T00:00:00+00:00")
        self._capture("16", "2026-11-01T00:00:00+00:00")
        self.assertEqual(self.ledger.pending_candidates()[0]["conflict_count"], 2)

    def test_approving_the_older_one_is_refused(self):
        old = self._capture("15", "2026-10-01T00:00:00+00:00")
        new = self._capture("16", "2026-11-01T00:00:00+00:00")
        with self.assertRaises(ValueError) as ctx:
            self.ledger.approve_candidate(old, "A", note="手滑")
        self.assertIn("#%d" % new, str(ctx.exception), "报错要指出是哪条更新的")
        self.assertAlmostEqual(self.ledger.spec.params["P-TW-COMMISSION"].value, 0.14, places=6)

    def test_force_allows_the_older_one(self):
        old = self._capture("15", "2026-10-01T00:00:00+00:00")
        self._capture("16", "2026-11-01T00:00:00+00:00")
        self.ledger.approve_candidate(old, "A", note="确认旧的才对", force=True)
        self.assertAlmostEqual(self.ledger.spec.params["P-TW-COMMISSION"].value, 0.15, places=6)

    def test_approving_the_newest_needs_no_force(self):
        self._capture("15", "2026-10-01T00:00:00+00:00")
        new = self._capture("16", "2026-11-01T00:00:00+00:00")
        self.ledger.approve_candidate(new, "A", note="用最新的")
        self.assertAlmostEqual(self.ledger.spec.params["P-TW-COMMISSION"].value, 0.16, places=6)



class UserscriptHeaderTest(unittest.TestCase):
    def test_version_header_matches_the_js_constant(self):
        """抬版本时 @version 与 VERSION 要一起改，否则 console 看不出装的是哪一版。"""
        path = Path(__file__).resolve().parents[1] / "tools" / "shopee-capture.user.js"
        text = path.read_text(encoding="utf-8")
        header = re.search(r"@version\s+([\d.]+)", text)
        constant = re.search(r"const VERSION = '([\d.]+)'", text)
        self.assertIsNotNone(header, "缺 @version")
        self.assertIsNotNone(constant, "缺 VERSION 常量")
        self.assertEqual(header.group(1), constant.group(1))

    def test_panel_has_manual_list_report_button(self):
        """每日去重会让当天无法再自动报送，面板必须留一个手动按钮。"""
        path = Path(__file__).resolve().parents[1] / "tools" / "shopee-capture.user.js"
        text = path.read_text(encoding="utf-8")
        self.assertIn("sl-list", text)
        self.assertIn("这是列表页", text)

    def test_every_panel_element_referenced_exists(self):
        """加了按钮却忘了取元素（或反过来），运行时就炸——没有 node 只能这样静态查。"""
        path = Path(__file__).resolve().parents[1] / "tools" / "shopee-capture.user.js"
        text = path.read_text(encoding="utf-8")
        declared = set(re.findall(r'id="(sl-[a-z]+)"', text))
        queried = set(re.findall(r"querySelector\('#(sl-[a-z]+)'\)", text))
        self.assertTrue(declared, "面板里没找到任何 sl- 元素")
        self.assertEqual(queried - declared, set(), "引用了不存在的面板元素")

    def test_uses_real_selectors_from_the_saved_page(self):
        """选择器必须来自真实 DOM，不能再靠猜。"""
        path = Path(__file__).resolve().parents[1] / "tools" / "shopee-capture.user.js"
        text = path.read_text(encoding="utf-8")
        for selector in ("li.article-item", "a.article-a", ".article-title", ".bottom-time",
                         "shopee-pager__page", "shopee-pager__button-next"):
            self.assertIn(selector, text, "缺少真实选择器 %s" % selector)

    def test_braces_and_parens_balance(self):
        path = Path(__file__).resolve().parents[1] / "tools" / "shopee-capture.user.js"
        text = path.read_text(encoding="utf-8")
        body = text.split("==/UserScript==", 1)[-1]
        for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(body.count(opener), body.count(closer),
                             "%s%s 不配对——JS 里大概率语法错误" % (opener, closer))
if __name__ == "__main__":
    unittest.main()
