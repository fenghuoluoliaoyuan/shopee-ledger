"""来源可达性记录：**关于证据的证据**。

为什么需要
----------
A 级的定义是「有可打开的 URL + 要点 + 日期」（见 .cursor/rules/handbook-review.mdc）。
但没有任何东西检查过那个 URL 到底打不打得开——实测发现 9 个 A 级参数里有 4 个
的来源指向 help.shopee.tw，而这个域名在当前网络上完全不可达（ERR_CONNECTION_TIMED_OUT）。

结论不是把它们悄悄降级（网络可能因代理/VPN 而变），而是**把可达性记下来并说出来**：
等级可以保留，但必须让使用的人知道"这条 A 级现在没法复核"。

记录落 ``spec/evidence/reachability.json``，是文件而非数据库——它描述的是证据本身，
要和被描述的证据放在一起，也要能被 git 跟踪。
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "spec" / "evidence" / "reachability.json"

# 失败分类。**证书问题与网络不通必须分开**：
# 证书验证失败说明站点是通的，只是本机信任库不认——浏览器往往能读。
# 混成一类会让"本来能拿到的来源"被当成拿不到（实测踩过：泰国税务那篇
# 因为 CERTIFICATE_VERIFY_FAILED 被标成不可达，搁置了好几轮，其实浏览器一读就有）。
CERT_ISSUE = "cert_issue"
UNREACHABLE = "unreachable"
CLIENT_ERROR = "client_error"
SERVER_ERROR = "server_error"
OK = "ok"

FAILURE_LABEL = {
    CERT_ISSUE: "证书验证失败（站点可能可通，用浏览器再试）",
    UNREACHABLE: "连不上",
    CLIENT_ERROR: "地址失效（4xx：站点是通的，但这个 URL 不行了）",
    SERVER_ERROR: "服务端错误（5xx）",
    OK: "通",
}


def classify_failure(exc: BaseException) -> str:
    """把抓取异常分成 cert_issue / unreachable / client_error / server_error / ok。

    注意 4xx 与 5xx 要分开：**404 说明站点是通的**，只是这个地址失效了；
    这和「服务器挂了」该采取的行动完全不同（前者要换来源，后者等一等再试）。
    """
    if isinstance(exc, ssl.SSLError):
        return CERT_ISSUE
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code < 400:
            return OK
        return CLIENT_ERROR if exc.code < 500 else SERVER_ERROR
    if isinstance(exc, (urllib.error.URLError, socket.timeout, OSError)):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLError):
            return CERT_ISSUE
        text = str(reason or exc).lower()
        if "certificate" in text or "ssl" in text:
            return CERT_ISSUE
        return UNREACHABLE
    return UNREACHABLE


def record_reachability(root: Path | str | None = None,
                        results: Iterable[tuple[str, str]] = (),
                        path: Path | str | None = None,
                        kind_by_url: dict[str, str] | None = None) -> Path:
    """把 (url, status) 记进可达性文件。同一 URL 覆盖旧记录，保留检查时间。

    ``kind_by_url`` 存分类（cert_issue / unreachable / ok），这样报告能区分
    「证书问题、浏览器可读」和「真的连不上」——两者该采取的行动完全不同。
    """
    target = Path(path) if path else (Path(root) if root else ROOT) / "spec" / "evidence" / "reachability.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"note": "来源 URL 可达性。A 级要求可打开的 URL，这里记录实测是否打得开。",
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "urls": {}}
    if target.exists():
        try:
            data["urls"] = json.loads(target.read_text(encoding="utf-8")).get("urls") or {}
        except ValueError:
            data["urls"] = {}
    kinds = kind_by_url or {}
    for url, status in results:
        entry = {"status": status,
                 "ok": status.startswith("HTTP"),
                 "checked_at": data["checked_at"]}
        if url in kinds:
            entry["kind"] = kinds[url]
            entry["kind_label"] = FAILURE_LABEL.get(kinds[url], "")
            # 证书问题不算"打不开"——站点是通的，只是本机验证不了
            if kinds[url] == CERT_ISSUE:
                entry["ok"] = False
                entry["hint"] = "用浏览器再试；Chrome 有自己的信任库"
        data["urls"][url] = entry
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_reachability(path: Path | str = DEFAULT_PATH) -> dict[str, dict]:
    """返回 {url: {ok, status, checked_at}}；文件不存在时返回空。"""
    target = Path(path)
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8")).get("urls") or {}
    except ValueError:
        return {}


def unreachable_for(url: str, records: dict[str, dict] | None = None) -> bool | None:
    """该 URL 是否已知打不开。没记录过返回 None（未知，不等于可达）。"""
    records = records if records is not None else load_reachability()
    item = records.get(url)
    if item is None:
        return None
    return not item.get("ok", False)
