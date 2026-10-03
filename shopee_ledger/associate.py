"""把抓到的文档自动关联到它可能回答的参数。

定位，不取值
------------
这一步只回答「**这篇文档里出现了这个参数的线索**」，绝不产出参数值。
取一个数值需要读懂原文的适用范围、生效日期、例外条款——那是判断，不是匹配。
（血泪史：按关键词取值，把「佣金直减10%」当成了佣金费率。）

它的价值是**把人从"110 篇里翻哪篇"变成"这三篇里挑"**。命中位置连上下文一起给出，
省掉来回找。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_KEYWORDS = Path(__file__).resolve().parent.parent / "spec" / "keywords.json"

# 命中位置前后各取多少字作为上下文
CONTEXT_BEFORE = 60
CONTEXT_AFTER = 140


@dataclass
class Hit:
    """一处命中：哪个关键词、在文档的哪个位置、上下文是什么。"""

    keyword: str
    url: str
    article_id: str
    title: str
    published_at: str
    text: str
    index: int

    @property
    def context(self) -> str:
        start = max(0, self.index - CONTEXT_BEFORE)
        snippet = self.text[start: self.index + CONTEXT_AFTER]
        return " ".join(snippet.split())


@dataclass
class Finding:
    """某个参数的一批候选文档。"""

    param_id: str
    hits: list[Hit] = field(default_factory=list)

    def by_document(self) -> dict[str, list[Hit]]:
        grouped: dict[str, list[Hit]] = {}
        for hit in self.hits:
            grouped.setdefault(hit.article_id, []).append(hit)
        return grouped

    @property
    def document_count(self) -> int:
        return len(self.by_document())


def load_keywords(path: Path | str = DEFAULT_KEYWORDS) -> dict[str, list[str]]:
    target = Path(path)
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    return {param: list(words) for param, words in (data.get("params") or {}).items()}


def associate(documents: list[dict[str, Any]], *,
              keywords: dict[str, list[str]] | None = None,
              only: str | None = None,
              max_documents: int = 8) -> list[Finding]:
    """documents 每项需要 article_id/title/published_at/text；返回按参数分组的命中。

    文档顺序决定优先级——调用方通常按发布日期倒序传入，于是**新文档优先**。
    """
    keywords = keywords if keywords is not None else load_keywords()
    params = [only] if only else sorted(keywords)
    findings: list[Finding] = []
    for param_id in params:
        words = keywords.get(param_id) or []
        if not words:
            continue
        finding = Finding(param_id)
        seen_documents: set[str] = set()
        for document in documents:
            text = document.get("text") or ""
            if not text:
                continue
            key = str(document.get("article_id") or document.get("url"))
            if key in seen_documents:
                continue
            best: Hit | None = None
            for word in words:
                index = text.find(word)
                if index < 0:
                    continue
                # 同一文档只留最靠前的那处，避免一个参数刷出一堆噪音
                if best is None or index < best.index:
                    best = Hit(keyword=word, url=document.get("url") or "",
                               article_id=str(document.get("article_id") or ""),
                               title=document.get("title") or "",
                               published_at=document.get("published_at") or "",
                               text=text, index=index)
            if best is not None:
                finding.hits.append(best)
                seen_documents.add(key)
                if len(finding.hits) >= max_documents:
                    break
        if finding.hits:
            findings.append(finding)
    return findings


def needs_evidence(param: Any) -> bool:
    """这个参数还需要取证吗。

    没有值、或等级是 D/E（经验值/未核实）→ 需要。
    已核实到 A/B/C 的不需要——把它们报成"没找到线索"会让人以为有缺口
    （实测 6 个 A 级参数被这么报过，而它们早已核实）。
    """
    if getattr(param, "value", None) is None:
        return True
    return getattr(param, "evidence_level", "E") in ("D", "E")


def coverage_report(findings: list[Finding], params: dict[str, Any]) -> dict[str, Any]:
    """汇总：哪些参数找到了线索、哪些一条都没有。

    分开两类：**还需要取证的**（可行动）与**已核实的**（只是没有"新"线索，不算缺口）。
    """
    with_hits = {finding.param_id for finding in findings}
    missing = [param_id for param_id in params if param_id not in with_hits]
    unverified = sorted(pid for pid in missing if needs_evidence(params[pid]))
    verified = sorted(pid for pid in missing if not needs_evidence(params[pid]))
    return {"with_hits": len(with_hits), "without_hits": unverified,
            "verified_without_hits": verified,
            "documents": sum(finding.document_count for finding in findings)}
