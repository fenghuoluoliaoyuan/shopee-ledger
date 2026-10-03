"""列表页监控的契约测试。

核心主张：**列表页只做发现，不产出任何参数值。**
"新文档出现了"是有用信号（费率变更通常新发一篇），但值必须另抓、另确认。
"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http.server import ThreadingHTTPServer  # noqa: E402

from shopee_ledger.store import Ledger  # noqa: E402
from shopee_ledger.watch import (  # noqa: E402
    diff_entries,
    entries_from_links,
    load_watches,
    normalize_date,
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


if __name__ == "__main__":
    unittest.main()
