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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "spec" / "evidence" / "reachability.json"


def record_reachability(root: Path | str | None = None,
                        results: Iterable[tuple[str, str]] = (),
                        path: Path | str | None = None) -> Path:
    """把 (url, status) 记进可达性文件。同一 URL 覆盖旧记录，保留检查时间。"""
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
    for url, status in results:
        data["urls"][url] = {"status": status,
                            "ok": status.startswith("HTTP"),
                            "checked_at": data["checked_at"]}
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
