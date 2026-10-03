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


if __name__ == "__main__":
    unittest.main()
