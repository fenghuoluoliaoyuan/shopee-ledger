"""列表页监控：只做**发现**，不做提取。

为什么发现层有价值
------------------
费率变更在实务上通常是**新发一篇通知**，而不是去改旧文章。
用户实测的「政策与物流」栏目一页就列了 10+ 篇带日期的通知（2026-08-03 ~ 2026-09-30）。
所以"有没有新文档"本身是有用信号——它比"盯死某一篇文章"更符合现实。

为什么本模块刻意不产出任何参数值
--------------------------------
1. 列表页没有正文，提取不了。
2. 就算能提取，"多篇文档提到同一个参数"还要处理新旧冲突：
   10 月的通知说 14%，11 月的通知说 16%，哪个算数？这必须有人确认。
3. 26619 那页已经演示过：整页出现 17 次「佣金」，没有一次是费率。
   跨文档批量提取会把假阳性放大 30 倍。

所以分工是：**列表页负责"该看哪篇"，配方负责"那篇里哪个数"，人负责"认不认"。**
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = ROOT / "spec" / "sources.json"

ARTICLE_RE = re.compile(r"/edu/article/(\d+)")
DATE_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")

# 导航/页脚链接也会指向 /article/，标题短得不正常。实测踩过：一条「iOS版」混进清单。
MIN_TITLE_LENGTH = 6
NAV_TITLES = {"ios版", "android版", "app下载", "更多", "首页"}

STATUS_NEW = "new"
STATUS_SEEN = "seen"


@dataclass
class WatchEntry:
    article_id: str
    title: str
    url: str
    published_at: str = ""

    def as_row(self) -> dict[str, Any]:
        return {"article_id": self.article_id, "title": self.title, "url": self.url,
                "published_at": self.published_at}


@dataclass
class Watch:
    id: str
    url: str
    note: str = ""
    access: str = "public"
    covers: list[str] = field(default_factory=list)


def load_watches(path: Path | str = DEFAULT_SOURCES) -> list[Watch]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Watch(id=item["id"], url=item["url"], note=item.get("note", ""),
                  access=item.get("access", "public"), covers=list(item.get("covers") or []))
            for item in doc.get("watch") or []]


class _TextExtractor(HTMLParser):
    """把渲染后的页面变成纯文本（丢掉 script/style）。

    ``region_only=True`` 时只抓正文容器内的文字。为什么必须这么做：
    整页文本里含网站导航，而导航里就有「禁售品政策」「商品上架规范」这类分类名——
    于是**每一篇文档都会命中这些关键词**，关联结果全是噪音（实测踩过）。
    """

    SKIP = {"script", "style", "noscript", "svg", "head"}
    REGION_HINTS = ("article-content", "articlecontent", "article-main-inner")
    BREAK_TAGS = ("p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "section")

    def __init__(self, region_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0
        self._depth = 0
        self._region_depth: int | None = None
        self._region_only = region_only
        self.found_region = False

    def _inside_region(self) -> bool:
        return (not self._region_only) or self._region_depth is not None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        self._depth += 1
        classes = (dict(attrs).get("class") or "").lower()
        if self._region_depth is None and any(hint in classes for hint in self.REGION_HINTS):
            self._region_depth = self._depth
            self.found_region = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._region_depth is not None and self._depth <= self._region_depth:
            self._region_depth = None
        self._depth = max(0, self._depth - 1)
        if tag in self.BREAK_TAGS and self._inside_region():
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._inside_region():
            return
        text = data.strip()
        if text:
            self.chunks.append(text + " ")


def _text_of(html: str, *, region_only: bool) -> tuple[str, bool]:
    parser = _TextExtractor(region_only=region_only)
    parser.feed(html or "")
    text = "".join(parser.chunks)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), parser.found_region


# 正文短于这个长度就认为容器没抓对，退回整页
MIN_REGION_LENGTH = 200


def article_text(html: str) -> str:
    """渲染后的文章页 → 纯文本。优先只取正文容器，避免导航污染关键词关联。

    容器没找对或内容过短时退回整页文本——宁可多带点噪音，也不要返回空。
    """
    if not html:
        return ""
    region, found = _text_of(html, region_only=True)
    if found and len(region) >= MIN_REGION_LENGTH:
        return region
    full, _ = _text_of(html, region_only=False)
    return full


def article_id_of(url: str) -> str | None:
    match = ARTICLE_RE.search(url or "")
    return match.group(1) if match else None


def normalize_date(text: str) -> str:
    """把 2026/9/3、2026-09-03、2026.9.3 统一成 2026-09-03。"""
    match = DATE_RE.search(text or "")
    if not match:
        return ""
    year, month, day = match.groups()
    return "%s-%02d-%02d" % (year, int(month), int(day))


class _ListingHTMLParser(HTMLParser):
    """从**渲染后的 HTML** 里抽列表条目与分页控件。

    选择器不是猜的——来自用户保存的真实页面（tests/fixtures/listing-page.html）：
        li.article-item > a.article-a[href]
                        + .article-title
                        + .article-title-bottom > .bottom-time
        .shopee-pagination > .shopee-pager__pages > li.shopee-pager__page(.active)
                           + button.shopee-pager__button-next
    用 stdlib 的 HTMLParser，不引第三方依赖。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict[str, str]] = []
        self.pages: list[str] = []
        self.current_page: str | None = None
        self.has_next = False
        self._entry: dict[str, str] | None = None
        self._grab: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrib = dict(attrs)
        classes = (attrib.get("class") or "").split()
        disabled = "disabled" in attrib

        if tag == "li" and "article-item" in classes:
            self._entry = {"href": "", "title": "", "date": ""}
            return
        if self._entry is not None:
            if tag == "a" and "article-a" in classes:
                self._entry["href"] = attrib.get("href") or ""
                return
            if "article-title" in classes and "article-title-bottom" not in classes:
                self._grab = "title"
                return
            if "bottom-time" in classes:
                self._grab = "date"
                return

        if "shopee-pager__button-next" in classes:
            self.has_next = not disabled
            self._grab = None
        elif "shopee-pager__page" in classes and "shopee-pager__dot" not in classes:
            self._grab = "active_page" if "active" in classes else "page"

    def handle_data(self, data: str) -> None:
        if self._entry is not None and self._grab in ("title", "date"):
            self._entry[self._grab] += data
        elif self._grab in ("page", "active_page"):
            text = data.strip()
            if text:
                self.pages.append(text)
                if self._grab == "active_page":
                    self.current_page = text
                self._grab = None

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._entry is not None:
            if self._entry["href"]:
                self.entries.append(self._entry)
            self._entry = None
            self._grab = None
            return
        if tag in ("div", "li", "span") and self._grab in ("title", "date", "page", "active_page"):
            self._grab = None


# 无头浏览器：列表页是 SPA，服务端取到的是空壳，必须让浏览器渲染。
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> str | None:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def render_page(url: str, *, browser: str | None = None, wait_ms: int = 12000,
                timeout: int = 120, runner: Any = None) -> tuple[str, str]:
    """渲染一个页面，返回 (HTML, 错误说明)。

    走 CDP（自己开的无头 Chrome），**等真实时间**。
    为什么不用 ``chrome --dump-dom``：加虚拟时钟会被永不结束的请求卡死，
    不加则页面没渲染完就 dump（拿到 JS 空壳）。只有等真实时间才稳。

    需要连续抓多个页面时，直接 ``with Chrome() as chrome`` 复用实例，别用这个函数。
    """
    if runner is not None:      # 测试注入
        return runner(url), ""
    from shopee_ledger.browser import BrowserError, Chrome

    try:
        with Chrome(browser=browser) as chrome:
            chrome.open(url, wait_seconds=max(1.0, wait_ms / 1000.0))
            html = chrome.html()
        return html, "" if html else "浏览器没有输出"
    except BrowserError as exc:
        return "", str(exc)
    except Exception as exc:  # 超时/崩溃都当成"这次没抓到"
        return "", "%s: %s" % (type(exc).__name__, exc)


def parse_listing_html(html: str, base_url: str = "") -> tuple[list[WatchEntry], dict[str, Any]]:
    """解析**渲染后的 HTML**（无头浏览器 dump、用户 Ctrl+S、或脚本发回的 outerHTML）。

    拿到真实 DOM 之后就不必再猜选择器了；这个函数就是「先取样本再写规则」的产物。
    返回 (条目, 分页信息)。
    """
    parser = _ListingHTMLParser()
    parser.feed(html or "")
    links = [{"href": item["href"], "text": item["title"], "date": item["date"]}
             for item in parser.entries]
    entries = entries_from_links(links, base_url=base_url)
    pager = {"current": parser.current_page, "pages": parser.pages,
             "has_next": parser.has_next, "page_count": len(parser.pages)}
    return entries, pager


def entries_from_links(links: list[dict[str, Any]], base_url: str = "") -> list[WatchEntry]:
    out: list[WatchEntry] = []
    seen: set[str] = set()
    for link in links:
        href = str(link.get("href") or "").strip()
        title = str(link.get("text") or "").strip()
        if not href or not title:
            continue
        if len(title) < MIN_TITLE_LENGTH or title.lower() in NAV_TITLES:
            continue  # 导航/页脚链接：标题短得不正常，不是通知
        if href.startswith("/") or not urlsplit(href).netloc:
            href = urljoin(base_url or "https://shopee.cn", href)
        article_id = article_id_of(href)
        if article_id is None or article_id in seen:
            continue
        seen.add(article_id)
        out.append(WatchEntry(article_id, title[:140], href,
                              normalize_date(link.get("date") or title)))
    return out


def diff_entries(previous: dict[str, dict[str, Any]],
                 current: list[WatchEntry]) -> dict[str, Any]:
    """和上次抓的比对。新增/标题变化/消失。**不比对数值**——列表页没有数值。"""
    current_ids = {entry.article_id for entry in current}
    new = [entry for entry in current if entry.article_id not in previous]
    changed = [entry for entry in current
               if entry.article_id in previous
               and (previous[entry.article_id].get("title") or "") != entry.title]
    gone = [article_id for article_id in previous if article_id not in current_ids]
    return {"new": new, "changed": changed, "gone": gone, "total": len(current)}


# 看起来像数据接口的路径特征——只用于**排序**，不用于过滤
API_HINTS = ("/api/", "/seh/", "article/list", "cat/list", "search", "list?")


def parse_api_calls(urls: list[str], limit: int = 30) -> list[str]:
    """保留浏览器报回来的全部 http(s) 请求，只去重并把"像接口的"排前面。

    **不要按关键词过滤**。上一版按 /api/、article/list 之类筛选，结果浏览器报回 3 个请求
    全被丢掉（真实接口路径和我猜的完全不同），而"过滤条件"本身就是猜测。
    脚本那边已经只挑 xmlhttprequest / fetch 发出来了，清单本来就很短——
    宁可全存下来自己看，也不要再用猜的条件筛一遍。
    """
    out: list[str] = []
    for url in urls or []:
        text = str(url or "").strip()
        if not text.startswith(("http://", "https://")):
            continue
        if text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    hinted = [url for url in out if any(hint in url.lower() for hint in API_HINTS)]
    return hinted + [url for url in out if url not in hinted]
