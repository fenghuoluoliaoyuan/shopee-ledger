"""文档→参数关联的契约测试。

最要紧的一条：**它只定位，不取值**。
按关键词取值是这个项目已经犯过的错——「佣金直减10%」被当成了佣金费率。
所以这里断言它产出的是"哪篇文档有线索"，而不是"值是多少"。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.associate import (  # noqa: E402
    DEFAULT_KEYWORDS,
    Finding,
    Hit,
    associate,
    coverage_report,
    load_keywords,
)


def document(article_id, title, text, published_at="2026-01-01"):
    return {"article_id": article_id, "title": title, "text": text,
            "published_at": published_at, "url": "https://shopee.cn/edu/article/%s" % article_id}


KEYWORDS = {"P-TEST": ["佣金费率"], "P-OTHER": ["无关词"]}


class AssociateTest(unittest.TestCase):
    def test_finds_the_document_that_mentions_the_keyword(self):
        docs = [document("1", "费率通知", "跨境直邮店铺佣金费率统一调整为14%（含税率）。"),
                document("2", "放假安排", "国庆期间打款顺延。")]
        findings = associate(docs, keywords=KEYWORDS, only="P-TEST")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].param_id, "P-TEST")
        self.assertEqual(findings[0].document_count, 1)
        self.assertEqual(findings[0].hits[0].article_id, "1")

    def test_never_returns_a_value(self):
        """定位与取值必须分开——这个对象不能有 value 字段。"""
        docs = [document("1", "费率", "佣金费率统一调整为14%。")]
        hit = associate(docs, keywords=KEYWORDS, only="P-TEST")[0].hits[0]
        self.assertFalse(hasattr(hit, "value"))
        self.assertFalse(hasattr(Finding("P-TEST"), "value"))
        self.assertNotIn("0.14", hit.context.replace("14%", ""), "不应从原文里解析出数值")

    def test_keyword_inside_a_promo_sentence_is_still_only_a_clue(self):
        """「佣金直减10%」必须只当线索——当年就是把它当成费率取值了。"""
        docs = [document("1", "免佣政策", "活动期间佣金直减10%，详情见附件。")]
        hit = associate(docs, keywords={"P-C": ["佣金"]}, only="P-C")[0].hits[0]
        self.assertEqual(hit.keyword, "佣金")
        self.assertIn("直减10%", hit.context, "上下文要原样给出，让读的人自己判断")

    def test_context_is_readable_and_trimmed(self):
        text = "前言。" * 50 + "佣金费率统一调整为14%。" + "后记。" * 50
        hit = associate([document("1", "t", text)], keywords=KEYWORDS, only="P-TEST")[0].hits[0]
        self.assertIn("佣金费率统一调整为14%", hit.context)
        self.assertLess(len(hit.context), 260, "上下文要截断，不能把整篇塞进报表")

    def test_document_order_is_respected_so_newest_wins(self):
        docs = [document("new", "新公告", "佣金费率有调整。", "2026-09-01"),
                document("old", "旧公告", "佣金费率有调整。", "2020-01-01")]
        hits = associate(docs, keywords=KEYWORDS, only="P-TEST")[0].hits
        self.assertEqual([hit.article_id for hit in hits], ["new", "old"])

    def test_one_document_counts_once_per_param(self):
        docs = [document("1", "t", "佣金费率…佣金费率…佣金费率…")]
        finding = associate(docs, keywords=KEYWORDS, only="P-TEST")[0]
        self.assertEqual(finding.document_count, 1)

    def test_max_documents_caps_the_list(self):
        docs = [document(str(index), "t", "佣金费率调整。") for index in range(20)]
        finding = associate(docs, keywords=KEYWORDS, only="P-TEST", max_documents=3)[0]
        self.assertEqual(finding.document_count, 3)

    def test_skips_documents_without_text(self):
        docs = [document("1", "t", ""), document("2", "t", None)]
        self.assertEqual(associate(docs, keywords=KEYWORDS, only="P-TEST"), [])

    def test_coverage_report_names_the_params_without_clues(self):
        docs = [document("1", "t", "佣金费率调整。")]
        findings = associate(docs, keywords=KEYWORDS)
        report = coverage_report(findings, KEYWORDS)
        self.assertEqual(report["with_hits"], 1)
        self.assertEqual(report["without_hits"], ["P-OTHER"])

    def test_load_keywords_reads_repo_file(self):
        self.assertTrue(DEFAULT_KEYWORDS.exists(), "spec/keywords.json 应当存在")
        keywords = load_keywords()
        self.assertIn("P-TW-DTS", keywords)
        self.assertIn("出货天数", keywords["P-TW-DTS"])

    def test_load_keywords_on_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            self.assertEqual(load_keywords(Path(folder) / "nope.json"), {})

    def test_every_keyword_param_exists_in_the_registry(self):
        """关键词表里不能有拼错的参数名——拼错了就静默不生效。"""
        from shopee_ledger.spec import default_spec

        known = set(default_spec().params)
        unknown = sorted(set(load_keywords()) - known)
        self.assertEqual(unknown, [], "关键词表里有参数名不在 spec 里")


if __name__ == "__main__":
    unittest.main()
