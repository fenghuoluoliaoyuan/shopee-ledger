"""快照体检的第二层：**文件在、进了版本库 ≠ 里面是正文**。

背景：2026-10-06 泰国税务那篇的来源被 WAF 拦了，196 字节的拦截页
（「您的请求可能存在威胁，已被拦截！」）被 `save_snapshot` 原样存成快照，
文件名与哈希都正常——只看"文件在不在、进没进 git"的检查一路绿灯。

这组测试钉两件事：真的拦截页要认出来；**真文章绝不能被认成拦截页**——
检查器误报会让人直接把它关掉（HANDOVER.md 坑 #7）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.snapshots import (  # noqa: E402
    check,
    looks_like_block_page,
)
from shopee_ledger.sources import (  # noqa: E402
    STATUS_HTTP_ERROR,
    STATUS_OK,
    Source,
    fetch_source,
    ingest_text,
)

ROOT = Path(__file__).resolve().parents[1]
GENUINE = ROOT / "data" / "snapshots" / "SRC-TH-VAT-811e93c49444.txt"

# 2026-10-06 实际存下来的 196 字节拦截页，逐字抄录（原文件已删，所以钉在这里）。
WAF_PAGE = """您的请求可能存在威胁，已被拦截！ 
EventID: fed8acb282ab4521a0406a755e4b88ba 
拦截时间: 2026-10-06 10:04 

如存在错误拦截，请联系网站管理员并提供 Event ID
"""

# 真文章里出现过的近义词——泛词检查器会在这三句上误报。
NEAR_MISSES = (
    "【卖家包装不规范拦截标准示例】",                      # ARTICLE-26569
    "- 是否存在威胁航空器安全飞行的因素",                  # ARTICLE-25237
    "相关包裹会被拦截处理，请您知悉。",                    # ARTICLE-28203
)


class BlockPageDetectionTest(unittest.TestCase):
    def test_the_archived_waf_page_is_detected(self):
        self.assertTrue(looks_like_block_page(WAF_PAGE))

    def test_a_waf_page_starting_the_line_elsewhere_is_still_detected(self):
        """标记不带头部（真实文件里有前导空白/换行），不能只认行首。"""
        self.assertTrue(looks_like_block_page("\n   " + WAF_PAGE))

    def test_real_article_text_is_not_detected(self):
        """真凭据不能被判成拦截页——用它旁边那份真文章原文。"""
        self.assertTrue(GENUINE.exists(), "真快照不该被删：%s" % GENUINE)
        text = GENUINE.read_text(encoding="utf-8")
        self.assertFalse(looks_like_block_page(text))

    def test_generic_words_alone_are_not_enough(self):
        """「拦截」「威胁」单独出现是正文措辞，不是 WAF 指纹。"""
        for line in NEAR_MISSES:
            self.assertFalse(looks_like_block_page(line), line)

    def test_long_text_quoting_the_waf_page_is_not_detected(self):
        """讲风控的文章会正常引用这些句子——正文字长就不该命中。"""
        article = "本文讲解 WAF 拦截原理。" + "详情见官方说明。" * 200 + WAF_PAGE
        self.assertGreater(len(article), 600)
        self.assertFalse(looks_like_block_page(article))

    def test_none_and_blank_are_not_block_pages(self):
        """判不出来 ≠ 是拦截页：没有文本时不能制造假警报。"""
        for text in (None, "", "   ", "\n\n\t  \r\n"):
            self.assertFalse(looks_like_block_page(text), repr(text))


class BlockPageHealthTest(unittest.TestCase):
    def test_a_block_page_health_is_not_usable(self):
        """接线：检测器认出的文件要真的让 SnapshotHealth 变成不可用。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            path = Path(folder) / "SRC-TH-VAT-078be12d8ece.txt"
            path.write_text(WAF_PAGE, encoding="utf-8")
            health = check(str(path))
        self.assertTrue(health.exists)
        self.assertTrue(health.block_page)
        self.assertFalse(health.usable, "拦截页不能当证据")
        self.assertIn("拦截页", health.detail)

    def test_the_genuine_snapshot_is_still_usable(self):
        """反向：真凭据（已跟踪 + 正文）不能因为这次改动变得不可用。"""
        health = check("data/snapshots/SRC-TH-VAT-811e93c49444.txt")
        self.assertTrue(health.exists)
        self.assertIs(health.tracked, True)
        self.assertFalse(health.block_page)
        self.assertTrue(health.usable)


class CaptureWiringTest(unittest.TestCase):
    """抓取路径：拦截页要**报失败**，而不是被存成一份"看起来像证据"的快照。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _source(self):
        return Source(id="SRC-TH-VAT", param_id="P-TH-VAT", url="https://example.test/vat",
                      access="public",
                      extract={"kind": "regex", "expr": "([0-9.]+)%", "group": 1})

    def _archived(self):
        folder = Path(self.tmp.name) / "snapshots"
        return list(folder.iterdir()) if folder.exists() else []

    def test_fetch_source_reports_the_block_page_and_saves_nothing(self):
        capture = fetch_source(self._source(), opener=lambda url: WAF_PAGE.encode(),
                               snapshot_dir=Path(self.tmp.name) / "snapshots")
        self.assertEqual(capture.status, STATUS_HTTP_ERROR)
        self.assertIsNone(capture.value)
        self.assertIsNone(capture.snapshot_ref, "拦截页不能当快照留档")
        self.assertEqual(self._archived(), [], "拦截页不该写进 data/snapshots")

    def test_ingest_text_reports_the_block_page_and_saves_nothing(self):
        """油猴路径：拦截页混进 STATUS_MANUAL 就是"人工留档的证据"，更不能存。"""
        capture = ingest_text(WAF_PAGE, param_id="P-TH-VAT",
                              snapshot_dir=Path(self.tmp.name) / "snapshots")
        self.assertEqual(capture.status, STATUS_HTTP_ERROR)
        self.assertIsNone(capture.snapshot_ref)
        self.assertEqual(self._archived(), [])

    def test_a_real_page_is_still_archived(self):
        """反向：正文照旧留档，别把整条抓取路径堵死。"""
        page = "泰国站点增值税率为 7%（含税）。" * 20
        capture = fetch_source(self._source(), opener=lambda url: page.encode(),
                               snapshot_dir=Path(self.tmp.name) / "snapshots")
        self.assertEqual(capture.status, STATUS_OK)
        self.assertAlmostEqual(capture.value, 7.0)
        self.assertTrue(capture.snapshot_ref)
        self.assertEqual(len(self._archived()), 1)


if __name__ == "__main__":
    unittest.main()
