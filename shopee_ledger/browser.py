"""最小 CDP 客户端：用 stdlib 驱动无头 Chrome。

为什么要自己写
--------------
``chrome --dump-dom`` 有两个死穴，实测都踩到了：
  · 加 ``--virtual-time-budget`` → 文章页有永不结束的请求，虚拟时钟走不完 → 卡死
  · 不加 → 页面还没渲染完就 dump，拿到的是 JS 空壳（12505 字节、20 字正文）
  · ``--timeout`` 在新版 Chrome 已被移除

所以自己开一个 Chrome（``--remote-debugging-port``），用 CDP 控制它：
导航、**等真实时间**、再取 DOM。顺带能做 ``--dump-dom`` 永远做不到的事——**点击**（翻页）。

只依赖 stdlib：socket 说 WebSocket，urllib 和 Chrome 握手。
参考的是 RFC 6455 的帧格式（客户端发必须加掩码、长度三种编码、支持分片）。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class BrowserError(RuntimeError):
    pass


# ---- WebSocket 帧（纯函数，可单测） ------------------------------------
def encode_frame(payload: bytes, opcode: int = OPCODE_TEXT, mask: bytes | None = None) -> bytes:
    """客户端发帧：必须加掩码（RFC 6455 §5.3）。"""
    if mask is None:
        mask = os.urandom(4)
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    header += bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header)


def decode_frame(data: bytes) -> tuple[int, bytes, int]:
    """解一帧，返回 (opcode, payload, 用掉的字节数)。服务端发的一般不加掩码。"""
    if len(data) < 2:
        raise BrowserError("帧太短")
    first, second = data[0], data[1]
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2
    elif length == 127:
        length = struct.unpack(">Q", data[offset:offset + 8])[0]
        offset += 8
    mask = None
    if masked:
        mask = data[offset:offset + 4]
        offset += 4
    payload = data[offset:offset + length]
    if len(payload) < length:
        raise BrowserError("帧不完整")
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload, offset + length


class WebSocket:
    """够用的 WebSocket 客户端：握手、发文本、收文本。"""

    def __init__(self, url: str, timeout: float = 30.0):
        if not url.startswith("ws://"):
            raise BrowserError("只支持 ws://，收到 %s" % url)
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host = host
        self.port = int(port or 80)
        self.path = "/" + path
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._handshake()
        self._buffer = b""

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (self.path, self.host, self.port, key)
        )
        self.sock.sendall(request.encode())
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("握手时连接被关闭")
            raw += chunk
        head, _, rest = raw.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise BrowserError("WebSocket 握手失败：%s" % head.split(b"\r\n")[0])
        self._buffer = rest

    def send_text(self, text: str) -> None:
        self.sock.sendall(encode_frame(text.encode("utf-8")))

    def _read_more(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise BrowserError("连接已关闭")
        self._buffer += chunk

    def recv_text(self, timeout: float | None = None) -> str:
        """收一条完整文本消息（自动处理 ping/pong 与分片）。"""
        if timeout is not None:
            self.sock.settimeout(timeout)
        parts: list[bytes] = []
        while True:
            while True:
                try:
                    opcode, payload, used = decode_frame(self._buffer)
                    self._buffer = self._buffer[used:]
                    break
                except BrowserError:
                    self._read_more()
            if opcode == OPCODE_PING:
                self.sock.sendall(encode_frame(payload, OPCODE_PONG))
                continue
            if opcode == OPCODE_CLOSE:
                raise BrowserError("对端关闭连接")
            parts.append(payload)
            # 简化：不追踪 fin 位，靠 JSON 能否解析判断消息是否完整
            try:
                return b"".join(parts).decode("utf-8")
            except UnicodeDecodeError:
                continue

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(b"", OPCODE_CLOSE))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class CDPSession:
    """一条 CDP 会话：发命令、按 id 收结果，自动跳过事件。"""

    def __init__(self, ws_url: str, timeout: float = 60.0):
        self.ws = WebSocket(ws_url, timeout=timeout)
        self.timeout = timeout
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}

    def call(self, method: str, params: dict[str, Any] | None = None,
             timeout: float | None = None) -> dict[str, Any]:
        message_id = self._next_id
        self._next_id += 1
        self.ws.send_text(json.dumps({"id": message_id, "method": method,
                                      "params": params or {}}))
        deadline = time.time() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise BrowserError("CDP 命令超时：%s" % method)
            text = self.ws.recv_text(timeout=max(0.5, remaining))
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    raise BrowserError("%s 失败：%s" % (method, message["error"]))
                return message.get("result") or {}
            if "id" in message:
                self._pending[message["id"]] = message

    def evaluate(self, expression: str, *, timeout: float = 30.0) -> Any:
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout)
        if result.get("exceptionDetails"):
            raise BrowserError("JS 报错：%s" % result["exceptionDetails"].get("text"))
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        self.ws.close()


def find_browser() -> str | None:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


class Chrome:
    """一个常驻的无头 Chrome。反复用同一个实例，比每次重启快得多。"""

    def __init__(self, port: int = 9333, profile: str | None = None,
                 browser: str | None = None, headless: bool = True):
        exe = browser or find_browser()
        if not exe:
            raise BrowserError("找不到 Chrome 或 Edge")
        self.port = port
        # profile 目录必须按端口区分：Chrome 会锁住 profile，两个实例共用就起不来。
        # 实测踩过——后台 harvest 在跑时，另一个端口的实例永远等不到调试端口。
        self.profile = profile or str(Path(os.environ.get("TEMP", ".")) / ("sl-cdp-%d" % port))
        args = [exe, "--headless=new" if headless else "--new-window",
                "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-sync",
                "--remote-debugging-port=%d" % port,
                "--user-data-dir=%s" % self.profile, "about:blank"]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        self.session: CDPSession | None = None
        self._wait_devtools()

    def _wait_devtools(self, timeout: float = 25.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % self.port, timeout=2) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                page = next((item for item in targets if item.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    self.session = CDPSession(page["webSocketDebuggerUrl"])
                    self.session.call("Page.enable")
                    self.session.call("Runtime.enable")
                    return
            except Exception:
                time.sleep(0.4)
        raise BrowserError("Chrome 的调试端口没起来")

    def open(self, url: str, *, wait_seconds: float = 6.0) -> None:
        """导航并等**真实时间**——不是虚拟时钟。这是绕开文章页卡死的关键。"""
        self.session.call("Page.navigate", {"url": url}, timeout=30)
        time.sleep(wait_seconds)

    def html(self) -> str:
        return self.session.evaluate("document.documentElement.outerHTML") or ""

    def click(self, selector: str) -> bool:
        return bool(self.session.evaluate(
            "(function(){var e=document.querySelector(%s);"
            "if(!e)return false;e.click();return true;})()" % json.dumps(selector)))

    def wait_for(self, expression: str, *, timeout: float = 15.0,
                 interval: float = 0.4) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.session.evaluate(expression):
                    return True
            except BrowserError:
                pass
            time.sleep(interval)
        return False

    def close(self) -> None:
        if self.session:
            self.session.close()
        try:
            self.process.terminate()
            self.process.wait(timeout=8)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass

    def __enter__(self) -> "Chrome":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
