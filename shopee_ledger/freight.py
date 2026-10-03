"""SLS 运费费率表：拉取、拉平、比对变化。

数据从哪来
----------
Shopee 跨境卖家自助服务站的**定价模拟器**有公开接口：

    /sellers/pricing-simulator/api/sls/site-config?date=YYYY-MM-DD

返回 9 个站点 × 货类（普货/特货）× 渠道的完整费率表，每档带生效与失效日期。

为什么要比对而不是只用
----------------------
费率会变（平台经常调）。原表能重拉，所以真正需要的不是"抓一次存下来"，
而是**发现它变了**。变了的判定要具体到"哪个站点哪个渠道哪一档从 X 变成 Y"，
否则等于没说。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = ROOT / "spec" / "reference"
API = "https://solutions.shopee.cn/sellers/pricing-simulator/api/sls/site-config"
FEES_API = "https://solutions.shopee.cn/sellers/pricing-simulator/api/site"
SIMULATOR_URL = "https://solutions.shopee.cn/sellers/pricing-simulator/"
# 实测值：成功时 code=200000，msg="ok"。别按 0 判成功。
SUCCESS_CODE = 200000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": SIMULATOR_URL,
}


def latest_reference(directory: Path | str = REFERENCE_DIR) -> Path | None:
    """仓库里最新的一份运费表快照。"""
    files = sorted(Path(directory).glob("sls-site-config-*.json"))
    return files[-1] if files else None


def fetch_site_config(date: str, *, opener: Callable[[str], Any] | None = None) -> dict:
    """拉取指定生效日期的运费费率表。"""
    url = "%s?date=%s" % (API, date)
    if opener is not None:
        raw = opener(url)
    else:
        request = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read()
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    payload = json.loads(text)
    # 实测：这个接口成功时 code = 200000（不是 0，也不是字符串 "ok"；"ok" 是 msg）。
    # 一开始按数字 0 判成功，导致每次都报"接口返回错误：ok"。
    code = payload.get("code")
    if code not in (SUCCESS_CODE, 0) and str(code).lower() != "ok":
        raise ValueError("接口返回错误：code=%s msg=%s" % (code, payload.get("msg")))
    return payload["data"]


def rate_cards(config: dict) -> dict[str, dict]:
    """把嵌套结构拉平成 {站点/货类/渠道/区域/付款方/模式: 费率字段}。

    拉平是为了比对——嵌套结构里"某个数字变了"很难说清，拉平后键就是位置。
    """
    cards: dict[str, dict] = {}
    for site in config.get("site_info") or []:
        site_name = site.get("name")
        for cargo in site.get("cargo_types") or []:
            for channel in cargo.get("channels") or []:
                for zone in channel.get("zones") or []:
                    for mode in zone.get("fee_modes") or []:
                        key = "/".join(str(part) for part in (
                            site_name, cargo.get("name"), channel.get("cn_name"),
                            zone.get("cn_name"), mode.get("type"), mode.get("name")))
                        cards[key] = {
                            "start_weight_g": mode.get("start_weight"),
                            "end_weight_g": mode.get("end_weight"),
                            "fee": mode.get("original_fee"),
                            "increment_unit_g": mode.get("increment_unit"),
                            "increment_amount": mode.get("increment_amount"),
                            "effective_date": mode.get("effective_date"),
                            "last_effective_date": mode.get("last_effective_date"),
                        }
    return cards


def diff_config(old: dict, new: dict, limit: int = 40) -> list[str]:
    """比对两份运费表，返回人话版的变化清单。空列表＝没变。"""
    before, after = rate_cards(old), rate_cards(new)
    changes: list[str] = []

    for key in sorted(set(before) | set(after)):
        if key not in after:
            changes.append("删除：%s" % key)
            continue
        if key not in before:
            card = after[key]
            changes.append("新增：%s（费率 %s）" % (key, card.get("fee")))
            continue
        old_card, new_card = before[key], after[key]
        diffs = ["%s %s→%s" % (field, old_card.get(field), new_card.get(field))
                 for field in ("fee", "start_weight_g", "end_weight_g",
                               "increment_unit_g", "increment_amount", "effective_date")
                 if old_card.get(field) != new_card.get(field)]
        if diffs:
            changes.append("修改：%s（%s）" % (key, "；".join(diffs)))
        if len(changes) >= limit:
            changes.append("…还有更多变化（已截断）")
            break

    if old.get("date") != new.get("date"):
        changes.insert(0, "生效日期：%s → %s" % (old.get("date"), new.get("date")))
    return changes


def diff_site_fees(old: list[dict], new: list[dict]) -> list[str]:
    """平台佣金率/手续费率的比对。"""
    before = {item["name"]: item for item in old or []}
    after = {item["name"]: item for item in new or []}
    changes = []
    for name in sorted(set(before) | set(after)):
        if name not in after:
            changes.append("删除站点：%s" % name)
        elif name not in before:
            changes.append("新增站点：%s" % name)
        else:
            for field in ("platform_commission", "handling_fee"):
                if before[name].get(field) != after[name].get(field):
                    changes.append("%s %s：%s → %s" % (name, field,
                                                       before[name].get(field),
                                                       after[name].get(field)))
    return changes
