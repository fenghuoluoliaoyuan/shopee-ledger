"""CDP 客户端的帧编解码测试。

这层最容易写错：客户端发帧**必须**加掩码（RFC 6455 §5.3），长度有 7 位 / 16 位 / 64 位
三种编码。写错了通常表现为"连上了但收不到回应"，很难查。
"""

import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.browser import (  # noqa: E402
    OPCODE_PONG,
    OPCODE_TEXT,
    BrowserError,
    decode_frame,
    encode_frame,
    find_browser,
)


class FrameTest(unittest.TestCase):
    def test_round_trip_small_payload(self):
        opcode, payload, used = decode_frame(encode_frame(b'{"id":1}'))
        self.assertEqual(opcode, OPCODE_TEXT)
        self.assertEqual(payload, b'{"id":1}')
        self.assertEqual(used, len(encode_frame(b'{"id":1}')))

    def test_client_frames_are_masked(self):
        """不加掩码的话 Chrome 会直接断开——这是最常见的错。"""
        frame = encode_frame(b"hello")
        self.assertTrue(frame[1] & 0x80, "掩码位必须是 1")
        mask = frame[2:6]
        self.assertNotEqual(mask, b"\x00\x00\x00\x00")
        body = frame[6:]
        self.assertEqual(bytes(b ^ mask[i % 4] for i, b in enumerate(body)), b"hello")

    def test_length_encodings(self):
        for size, expected_extra in ((10, 0), (300, 2), (70000, 8)):
            frame = encode_frame(b"x" * size)
            length_bits = frame[1] & 0x7F
            if size < 126:
                self.assertEqual(length_bits, size)
            elif size < 65536:
                self.assertEqual(length_bits, 126)
                self.assertEqual(struct.unpack(">H", frame[2:4])[0], size)
            else:
                self.assertEqual(length_bits, 127)
                self.assertEqual(struct.unpack(">Q", frame[2:10])[0], size)
            opcode, payload, _ = decode_frame(frame)
            self.assertEqual(len(payload), size)

    def test_decode_server_frame_without_mask(self):
        payload = b"no mask here"
        raw = bytes([0x80 | OPCODE_TEXT, len(payload)]) + payload
        opcode, decoded, used = decode_frame(raw)
        self.assertEqual(opcode, OPCODE_TEXT)
        self.assertEqual(decoded, payload)
        self.assertEqual(used, len(raw))

    def test_decode_rejects_incomplete_frame(self):
        with self.assertRaises(BrowserError):
            decode_frame(b"\x81")

    def test_custom_mask_is_reproducible(self):
        frame = encode_frame(b"abc", mask=b"\x01\x02\x03\x04")
        self.assertEqual(frame[2:6], b"\x01\x02\x03\x04")
        self.assertEqual(decode_frame(frame)[1], b"abc")

    def test_pong_opcode_survives_round_trip(self):
        opcode, payload, _ = decode_frame(encode_frame(b"ping-data", OPCODE_PONG))
        self.assertEqual(opcode, OPCODE_PONG)
        self.assertEqual(payload, b"ping-data")

    def test_find_browser_finds_something_on_this_machine(self):
        self.assertTrue(find_browser(), "这台机器上应当有 Chrome 或 Edge")

    def test_article_text_strips_markup_and_keeps_content(self):
        """自己读文档找参数值靠这个函数，别把 script/style 里的噪音带进来。"""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from shopee_ledger.watch import article_text

        html = ("<html><head><style>body{color:red}</style>"
                "<script>var x=1;</script></head><body>"
                "<nav>导航</nav><h1>费率通知</h1>"
                "<p>跨境直邮店铺佣金费率统一调整为14%（含税率）。</p>"
                "</body></html>")
        text = article_text(html)
        self.assertIn("跨境直邮店铺佣金费率统一调整为14%", text)
        self.assertIn("费率通知", text)
        self.assertNotIn("var x=1", text)
        self.assertNotIn("color:red", text)

    def test_article_text_handles_empty_input(self):
        from shopee_ledger.watch import article_text

        self.assertEqual(article_text(""), "")
        self.assertEqual(article_text(None), "")

    def test_article_text_prefers_the_content_container_over_the_whole_page(self):
        """整页文本含网站导航，导航里的分类名会污染参数关联——实测踩过。"""
        from shopee_ledger.watch import article_text

        body = "跨境直邮店铺佣金费率统一调整为14%（含税率），自2026年1月1日起生效。" * 6
        html = (
            '<html><body><nav><a>禁售品政策</a><a>商品上架规范</a>'
            '<a>新手须知规则</a></nav>'
            '<div class="article-detail-wrap"><div class="article-main-inner">'
            '<h1>费率调整通知</h1><div class="article-content ql-container ql-snow">'
            + body + '</div></div></div>'
            '<footer>关于Shopee 关注我们</footer></body></html>')
        text = article_text(html)
        self.assertIn("跨境直邮店铺佣金费率统一调整为14%", text)
        self.assertNotIn("禁售品政策", text, "导航分类名不该混进正文")
        self.assertNotIn("商品上架规范", text)
        self.assertNotIn("关注我们", text, "页脚也不该混进来")

    def test_article_text_falls_back_to_whole_page_when_region_is_missing(self):
        from shopee_ledger.watch import article_text

        html = "<html><body><div class='something'>" + "正文内容。" * 60 + "</div></body></html>"
        self.assertIn("正文内容", article_text(html))

    def test_article_text_falls_back_when_region_is_too_short(self):
        """容器找对了但里面几乎是空的——宁可用整页，也不要返回空。"""
        from shopee_ledger.watch import article_text

        html = ('<html><body><div class="article-content">短</div>'
                '<div class="other">' + "真正的正文在这里。" * 40 + "</div></body></html>")
        self.assertIn("真正的正文在这里", article_text(html))


class RenderFailedTest(unittest.TestCase):
    """浏览器把网络错误也渲染成页面——"Chrome 返回了 HTML" ≠ "页面加载成功"。

    实测踩过两次：
    1. 只看 html[:4000]——Chrome 错误页前面塞了 187KB 内联 base64 CSS，错误码在后面，检测形同虚设；
    2. 改成全文之后又只看"去标签"，而 style 里的 CSS 是**文本内容**，去标签去不掉，
       "可见文本"有十几万字，长度判断永远不成立。
    """

    def _error_page(self) -> str:
        css = "<style>" + "a{color:red}" * 20000 + "</style>"
        body = ("<body><h1>无法访问此网站</h1><p>help.shopee.tw 拒绝了我们的连接请求。</p>"
                "<span>ERR_CONNECTION_REFUSED</span></body>")
        return "<html><head>" + css + "</head>" + body + "</html>"

    def test_detects_an_error_page_with_a_huge_inline_stylesheet(self):
        from shopee_ledger.browser import render_failed

        self.assertEqual(render_failed(self._error_page()), "ERR_CONNECTION_REFUSED")

    def test_style_and_script_content_is_not_counted_as_visible_text(self):
        from shopee_ledger.browser import visible_text

        html = ("<html><head><style>" + "x" * 5000 + "</style>"
                "<script>" + "y" * 5000 + "</script></head><body>真正的正文</body></html>")
        self.assertEqual(visible_text(html).strip(), "真正的正文")

    def test_a_long_troubleshooting_article_is_not_an_error_page(self):
        """讲排障的文章会正常提到 ERR_ 码，不能因此判成错误页。"""
        from shopee_ledger.browser import render_failed

        article = "<html><body>" + "排查连接问题的方法。" * 400 + "ERR_CONNECTION_REFUSED</body></html>"
        self.assertIsNone(render_failed(article))

    def test_a_normal_long_page_is_not_an_error_page(self):
        from shopee_ledger.browser import render_failed

        self.assertIsNone(render_failed("<html><body>" + "正文" * 3000 + "</body></html>"))

    def test_empty_html_is_reported_as_empty(self):
        from shopee_ledger.browser import render_failed

        self.assertEqual(render_failed(""), "EMPTY")
        self.assertEqual(render_failed(None), "EMPTY")

    def test_various_error_codes(self):
        from shopee_ledger.browser import render_failed

        for code in ("ERR_CONNECTION_TIMED_OUT", "ERR_NAME_NOT_RESOLVED",
                     "ERR_EMPTY_RESPONSE", "ERR_SSL_PROTOCOL_ERROR"):
            html = "<html><body>无法访问此网站 %s</body></html>" % code
            self.assertEqual(render_failed(html), code)


if __name__ == "__main__":
    unittest.main()
