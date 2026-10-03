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


def parse_listing_html(html: str, base_url: str = "") -> tuple[list[WatchEntry], dict[str, Any]]:
    """解析**渲染后的 HTML**（用户 Ctrl+S 存下来的，或脚本发回来的 outerHTML）。

    拿到真实 DOM 之后就不必再猜选择器了；这个函数就是"先取样本再写规则"的产物。
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
