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
from urllib.parse import parse_qsl, urlsplit

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


# 这些查询参数只跟来源追踪有关，比对页面身份时忽略
TRACKING_PREFIXES = ("utm_", "from", "share_", "spm", "scm", "ref", "fbclid")


def page_key(url: str) -> tuple:
    """把 URL 归一成 (主机, 路径, 查询) 用于比对页面身份。

    查询串**必须保留**：shopee.cn/edu/category?sub_cat_id=1066 与 ?sub_cat_id=1077
    是两页不同内容。只忽略来源追踪类参数（utm_* 等）。
    主机忽略 www、路径忽略结尾斜杠。
    """
    parsed = urlsplit(url or "")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = tuple(sorted(
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(TRACKING_PREFIXES)))
    return host, parsed.path.rstrip("/"), query


@dataclass
class Source:
    id: str
    param_id: str
    url: str
    access: str = "public"        # public | login | blocked
    review_cycle: str = "quarterly"
    note: str = ""
    extract: dict[str, Any] = field(default_factory=dict)
    # access == "blocked" 时说明为什么打不开——缺口要留痕，不能靠"跳过"掩盖
    blocked_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"id": self.id, "param_id": self.param_id, "url": self.url,
               "access": self.access, "review_cycle": self.review_cycle, "note": self.note}
        if self.blocked_reason:
            out["blocked_reason"] = self.blocked_reason
        return out


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
        blocked_reason=item.get("blocked_reason", ""),
    ) for item in doc.get("sources") or []]


def scope_text(text: str, scope: dict[str, Any] | None) -> str:
    """先锁定页面里的那一段，再在里面提取。

    为什么必须有这一步：shopee.cn/edu/article/26620 一页写了 6 个站点，
    「14%」在新加坡（MY-SG 项目）和台湾各出现一次。只在整页上跑正则，
    抓对是运气，抓错是常态。作用域把"哪一段"这件事显式写进配方。

    ``after`` 找不到 → 返回空串（提取必然失败）。宁可报失败，也不要退化成全页匹配。
    """
    if not scope:
        return text
    start = 0
    after = scope.get("after")
    if after:
        index = text.find(after)
        if index < 0:
            return ""
        start = index + len(after)
    end = len(text)
    before = scope.get("before")
    if before:
        index = text.find(before, start)
        if index > 0:
            end = index
    window = scope.get("window")
    if window:
        end = min(end, start + int(window))
    return text[start:end]


def extract_value(rule: dict[str, Any], text: str) -> Any:
    """按配方提取。提取不到就返回 None——绝不返回一个"看起来对"的默认值。"""
    kind = (rule or {}).get("kind")
    if kind == "regex":
        scoped = scope_text(text, rule.get("scope"))
        if not scoped:
            return None
        match = re.search(rule["expr"], scoped, re.S)
        if not match:
            return None
        try:
            raw = match.group(rule.get("group", 1))
        except IndexError:
            # 配方写错了（正则里没有这个捕获组）。报失败即可，不该把整次抓取炸掉。
            return None
        raw = raw.replace(",", "").strip()
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
    renderer: Callable[[str], str] | None = None,
) -> Capture:
    """抓一个公开来源。需登录/纯手工的来源直接返回对应状态，不做任何臆测。

    ``renderer`` 是"把 URL 渲染成 HTML"的函数（无头浏览器）。给了它就会在直取失败后
    退回浏览器——实测 shopee.cn/edu 是 JS 空壳，直取永远拿不到正文，只有浏览器能渲染。
    """
    if source.access == "login" and renderer is None:
        return Capture(source.id, source.param_id, source.url, STATUS_NEEDS_LOGIN,
                       message="需登录：请在浏览器里用油猴脚本抓，或人工抄录")
    if (source.extract or {}).get("kind") == "manual":
        return Capture(source.id, source.param_id, source.url, STATUS_MANUAL,
                       message="配方标为手工：抓取器不处理")

    if source.access == "login":
        return Capture(source.id, source.param_id, source.url, STATUS_NEEDS_LOGIN,
                       message="需登录：请在浏览器里用油猴脚本抓，或人工抄录")

    text, via = "", "直取"
    try:
        if opener is None:
            request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        else:
            raw = opener(source.url)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        if renderer is None:
            return Capture(source.id, source.param_id, source.url, STATUS_HTTP_ERROR,
                           message="请求失败：%s" % exc)
        text = ""

    # 直取失败、或直取到了但提取不出（典型：JS 空壳页），改用浏览器渲染
    need_render = (not text) or extract_value(source.extract, text) is None
    if need_render and renderer is not None:
        try:
            rendered = renderer(source.url) or ""
        except Exception as exc:
            rendered = ""
            if not text:
                return Capture(source.id, source.param_id, source.url, STATUS_HTTP_ERROR,
                               message="渲染失败：%s" % exc)
        if rendered:
            # 浏览器会把网络错误也渲染成页面，先认出错误页：
            # "Chrome 返回了 HTML" 不等于 "页面加载成功"。
            from shopee_ledger.browser import render_failed

            failure = render_failed(rendered)
            if failure and not text:
                return Capture(source.id, source.param_id, source.url, STATUS_HTTP_ERROR,
                               message="浏览器也没打开这页：%s" % failure)
            if not failure:
                # 关键：渲染回来的是 HTML，提取正则是在**纯文本**上写的。
                # 实测 #26620 的 HTML 里标签插在「佣金费率统一调整为」和「16%」之间，
                # 直接在 HTML 上跑正则永远匹配不上。
                from shopee_ledger.watch import article_text

                rendered_text = article_text(rendered)
                # **渲染成功就用它的文本，不管提取成不成功**。
                # 之前这里是「提取成功才采纳」，于是提取失败时渲染成果被整个丢掉：
                # 快照不存、状态还误报成 http_error——明明拿到了页面。
                if rendered_text.strip():
                    text, via = rendered_text, "浏览器渲染"

    if not text:
        return Capture(source.id, source.param_id, source.url, STATUS_HTTP_ERROR,
                       message="直取和浏览器渲染都没拿到内容")

    snapshot_ref, digest = save_snapshot(text, source.id, snapshot_dir)
    value = extract_value(source.extract, text)
    if value is None:
        # 关键：抓到了页面但没提取出数字 → 报失败并留下快照，等人去修配方
        return Capture(source.id, source.param_id, source.url, STATUS_EXTRACT_FAILED,
                       snapshot_ref=snapshot_ref, raw_sha256=digest, raw_length=len(text),
                       message="页面抓到了但配方没匹配上（可能是 JS 空壳页或规则过期），已存快照待人工核对")
    return Capture(source.id, source.param_id, source.url, STATUS_OK, value=value,
                   snapshot_ref=snapshot_ref, raw_sha256=digest, raw_length=len(text),
                   message="来自%s的页面（%d 字）" % (via, len(text)))


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
    """先按参数 ID 精确匹配，URL 只作兜底。

    顺序不能反：一页可以登记多条配方（shopee.cn/edu/article/26620 一页就有佣金、
    交易手续费、预售服务费三条，URL 完全相同）。先按 URL 匹配会让后两条都命中第一条，
    于是三个参数全变成佣金率——测过，真的会。
    """
    if param_id:
        for source in sources:
            if source.param_id == param_id:
                return source
    if url:
        for source in sources:
            if page_key(source.url) == page_key(url):
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
        # 没有配方也要**把正文留档**。登录门禁的页面（入驻须知、当单页字段、打款规则等）
        # 配方写不出来——它们是散文不是数字，但正文本身就是证据。
        # 丢掉它等于把用户唯一能提供的东西扔了：他登录一次不容易，
        # 而"读数"这件事不需要他在场。
        snapshot_ref, digest = save_snapshot(text, param_id or "unmapped", snapshot_dir)
        if param_id:
            return Capture(param_id, param_id, url or "", STATUS_MANUAL,
                           channel=channel, snapshot_ref=snapshot_ref,
                           raw_sha256=digest, raw_length=len(text),
                           captured_at=captured_at or "",
                           message="已留档正文 %d 字，但没有配方——需要人工读数（或补一条配方）"
                                   % len(text))
        return Capture("?", "?", url or "", STATUS_NO_RECIPE, channel=channel,
                       snapshot_ref=snapshot_ref, raw_sha256=digest, raw_length=len(text),
                       captured_at=captured_at or "",
                       message="没说这是哪个参数的页面；正文已留档，但不知道该读成什么")

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
