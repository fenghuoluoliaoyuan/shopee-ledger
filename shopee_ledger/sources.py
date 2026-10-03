"""抓取配方与候选值采集。

三条纪律（写进代码，不靠自觉）
------------------------------
1. **抓不到就报抓不到**。提取失败返回 ``value=None`` + 原始快照留档，
   绝不"猜一个看起来合理的数字"。这跟参数缺值不许当 0 是同一条纪律。
2. **抓到的不是系统依据**。产出只写进 ``ParamCandidate``（pending），
   人工 approve 之后才成为 ``ParamOverride``。平台悄悄改费率时，系统的决策不能跟着悄悄变。
3. **快照必须留档**。``P-GOV-SNAPSHOT`` 要求 A 级来源可追溯，抓取顺手把原文按 sha256 存下来。

分工
----
* ``access=public`` → 服务端直接抓（本模块）
* ``access=login``  → 需登录的页面，由油猴脚本在浏览器里抓，POST 到本机 ``/ingest``
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = ROOT / "spec" / "sources.json"
DEFAULT_SNAPSHOT_DIR = ROOT / "data" / "snapshots"

USER_AGENT = "shopee-ledger/0.1 (personal bookkeeping; contact: local)"

STATUS_OK = "ok"
STATUS_EXTRACT_FAILED = "extract_failed"
STATUS_HTTP_ERROR = "http_error"
STATUS_NEEDS_LOGIN = "needs_login"
STATUS_MANUAL = "manual_required"
STATUS_NO_RECIPE = "no_recipe"
STATUS_URL_MISMATCH = "url_mismatch"


def page_key(url: str) -> tuple[str, str]:
    """把 URL 归一成 (主机, 路径) 用于比对，忽略 www 和结尾斜杠。"""
    parsed = urlsplit(url or "")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host, parsed.path.rstrip("/")


@dataclass
class Source:
    id: str
    param_id: str
    url: str
    access: str = "public"
    review_cycle: str = "quarterly"
    note: str = ""
    extract: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "param_id": self.param_id, "url": self.url,
                "access": self.access, "review_cycle": self.review_cycle, "note": self.note}


@dataclass
class Capture:
    """一次采集的结果。没有 value 就是没抓到，不允许用别的东西顶替。"""

    source_id: str
    param_id: str
    url: str
    status: str
    channel: str = "fetch"
    value: Any = None
    snapshot_ref: str | None = None
    raw_sha256: str | None = None
    raw_length: int = 0
    captured_at: str = ""
    message: str = ""
    expected_url: str | None = None   # 配方登记的页面；与 url 不同即视为证据链不成立

    def __post_init__(self) -> None:
        if not self.captured_at:
            self.captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and self.value is not None

    def as_row(self) -> dict[str, Any]:
        return {
            "param_id": self.param_id, "value": self.value, "source_id": self.source_id,
            "source_url": self.url, "snapshot_ref": self.snapshot_ref,
            "raw_sha256": self.raw_sha256, "captured_at": self.captured_at,
            "channel": self.channel, "status": "pending", "message": self.message,
        }

    def describe(self) -> str:
        if self.ok:
            return "%s ← %s" % (self.param_id, self.value)
        return "%s：%s（%s）" % (self.param_id, self.status, self.message or "无值")


def load_sources(path: Path | str = DEFAULT_SOURCES) -> list[Source]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Source(
        id=item["id"], param_id=item["param_id"], url=item["url"],
        access=item.get("access", "public"), review_cycle=item.get("review_cycle", "quarterly"),
        note=item.get("note", ""), extract=item.get("extract") or {},
    ) for item in doc.get("sources") or []]


def extract_value(rule: dict[str, Any], text: str) -> Any:
    """按配方提取。提取不到就返回 None——绝不返回一个"看起来对"的默认值。"""
    kind = (rule or {}).get("kind")
    if kind == "regex":
        match = re.search(rule["expr"], text, re.S)
        if not match:
            return None
        raw = match.group(rule.get("group", 1)).replace(",", "").strip()
        try:
            number = float(raw)
        except ValueError:
            return None
        scale = rule.get("scale", 1)
        scaled = number * scale
        if scale == 1 and float(scaled).is_integer():
            return int(scaled)
        # 必须 round：14 * 0.01 在 IEEE754 里是 0.14000000000000001，会让"值没变"被判成"变了"
        return round(scaled, 12)
    if kind == "json":
        node: Any = json.loads(text)
        for part in str(rule["expr"]).split("."):
            node = node[int(part)] if part.isdigit() else node[part]
        return node
    return None


def save_snapshot(text: str, source_id: str, snapshot_dir: Path | str = DEFAULT_SNAPSHOT_DIR) -> tuple[str, str]:
    """按内容哈希存档，返回 (相对路径, sha256)。同一内容重复抓取不会写第二份。"""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    folder = Path(snapshot_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%s-%s.txt" % (source_id, digest[:12]))
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    try:
        shown = str(path.relative_to(ROOT))
    except ValueError:
        shown = str(path)
    return shown, digest


def fetch_source(
    source: Source,
    *,
    opener: Callable[[str], Any] | None = None,
    timeout: int = 15,
    snapshot_dir: Path | str = DEFAULT_SNAPSHOT_DIR,
) -> Capture:
    """抓一个公开来源。需登录/纯手工的来源直接返回对应状态，不做任何臆测。"""
    if source.access == "login":
        return Capture(source.id, source.param_id, source.url, STATUS_NEEDS_LOGIN,
                       message="需登录：请在浏览器里用油猴脚本抓，或人工抄录")
    if (source.extract or {}).get("kind") == "manual":
        return Capture(source.id, source.param_id, source.url, STATUS_MANUAL,
                       message="配方标为手工：抓取器不处理")

    try:
        if opener is None:
            request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        else:
            raw = opener(source.url)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return Capture(source.id, source.param_id, source.url, STATUS_HTTP_ERROR,
                       message="请求失败：%s" % exc)

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    snapshot_ref, digest = save_snapshot(text, source.id, snapshot_dir)
    value = extract_value(source.extract, text)
    if value is None:
        # 关键：抓到了页面但没提取出数字 → 报失败并留下快照，等人去修配方
        return Capture(source.id, source.param_id, source.url, STATUS_EXTRACT_FAILED,
                       snapshot_ref=snapshot_ref, raw_sha256=digest, raw_length=len(text),
                       message="页面抓到了但配方没匹配上（可能是 JS 空壳页或规则过期），已存快照待人工核对")
    return Capture(source.id, source.param_id, source.url, STATUS_OK, value=value,
                   snapshot_ref=snapshot_ref, raw_sha256=digest, raw_length=len(text))


def fetch_all(
    sources: list[Source] | None = None,
    *,
    only_public: bool = True,
    opener: Callable[[str], Any] | None = None,
    snapshot_dir: Path | str = DEFAULT_SNAPSHOT_DIR,
) -> list[Capture]:
    items = sources if sources is not None else load_sources()
    results = []
    for source in items:
        if only_public and source.access != "public":
            continue
        results.append(fetch_source(source, opener=opener, snapshot_dir=snapshot_dir))
    return results


def find_source(sources: list[Source], *, param_id: str | None = None,
                url: str | None = None) -> Source | None:
    for source in sources:
        if param_id and source.param_id == param_id:
            return source
        if url and source.url.rstrip("/") == url.rstrip("/"):
            return source
    return None


def ingest_text(
    text: str,
    *,
    param_id: str | None = None,
    url: str | None = None,
    captured_at: str | None = None,
    channel: str = "userscript",
    sources: list[Source] | None = None,
    snapshot_dir: Path | str = DEFAULT_SNAPSHOT_DIR,
) -> Capture:
    """处理浏览器送回来的**渲染后文本**。

    这是油猴脚本的入口：浏览器执行了 JS、带了登录态、有完整证书链，
    而提取规则仍然只在 spec 里定义一次——同一份配方，两个执行环境。
    """
    items = sources if sources is not None else load_sources()
    source = find_source(items, param_id=param_id, url=url)
    if source is None:
        return Capture(param_id or url or "?", param_id or "?",
                       url or "", STATUS_NO_RECIPE,
                       channel=channel, message="spec/sources.json 里没有对应配方，未提取")

    page_url = (url or source.url).strip() or source.url
    snapshot_ref, digest = save_snapshot(text, source.id, snapshot_dir)

    # 证据链检查：文本必须来自配方登记的那个页面。
    # 少了这一步，在 A 页面抓的文本会被记成"来自 B 页面"——审计就指向了错误的来源。
    if url and page_key(url) != page_key(source.url):
        return Capture(source.id, source.param_id, page_url, STATUS_URL_MISMATCH,
                       channel=channel, snapshot_ref=snapshot_ref, raw_sha256=digest,
                       raw_length=len(text), captured_at=captured_at or "",
                       expected_url=source.url,
                       message="你打开的页面是这个，但配方是给 %s 写的；这里不提取。"
                               "要抓这个页面，就给它单独加一条配方。" % source.url)

    value = extract_value(source.extract, text)
    if value is None:
        return Capture(source.id, source.param_id, page_url, STATUS_EXTRACT_FAILED,
                       channel=channel, snapshot_ref=snapshot_ref, raw_sha256=digest,
                       raw_length=len(text), captured_at=captured_at or "",
                       expected_url=source.url,
                       message="渲染后的文本里也没匹配上配方；快照已存，去修规则或人工抄")
    return Capture(source.id, source.param_id, page_url, STATUS_OK,
                   channel=channel, value=value, snapshot_ref=snapshot_ref,
                   raw_sha256=digest, raw_length=len(text), captured_at=captured_at or "",
                   expected_url=source.url, message="来自浏览器渲染后的页面")


def sources_payload(sources: list[Source] | None = None) -> list[dict[str, Any]]:
    """给油猴脚本的配方清单（下拉框据此生成，不用在脚本里重复写一遍）。"""
    return [dict(item.as_dict(),
                 extract_kind=(item.extract or {}).get("kind"),
                 extract_hint=(item.extract or {}).get("expr")) for item in (sources or load_sources())]
