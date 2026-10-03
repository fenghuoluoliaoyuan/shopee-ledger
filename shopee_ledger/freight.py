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
import math
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
                        # 区间也要进 key！同一渠道的多个 Increment 档 payer/mode 完全相同，
                        # 只用 site/cargo/channel/zone/payer/mode 做键会互相覆盖——
                        # 实测把 6 档压成 3 档，变化检测就会漏掉中间档的费率变动。
                        key = "/".join(str(part) for part in (
                            site_name, cargo.get("name"), channel.get("cn_name"),
                            zone.get("cn_name"), mode.get("type"), mode.get("name"),
                            mode.get("start_weight"), mode.get("end_weight")))
                        cards[key] = {
                            # payer/mode 也放进值里：按重量取费时要用它们筛选，
                            # 只放在 key 里就得反解析字符串，容易错
                            "payer": mode.get("type"),
                            "mode": mode.get("name"),
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


# ---- 按重量算运费 -------------------------------------------------------
# 算法不是猜的：从 Shopee 定价模拟器自己的前端包 app.bb24a38e.js 里读出来的
# （函数 ke(weight, fee_modes)）。要点：
#   · 先按 type 分出 Seller / Buyer；
#   · 该付款方若有 Flat 档 → 直接取 original_fee，不做累加；
#   · 否则把**所有**满足 weight > start_weight 的档累加：
#       WeightRange → + original_fee
#       Increment   → + ceil((min(weight, end_weight) - start_weight) / increment_unit)
#                       * increment_amount
#   原代码里有一句 sort 把两个对象的字段混着比（t.end_weight 减 e.start_weight），
#   但因为总量是对所有命中档求和，顺序不影响结果——所以这里不复制那句可疑的排序。
#
# 边界语义照抄原码：判定是**严格大于** start_weight。所以恰好 500g 命中首档，
# 而 500.01g 反而不命中 500.01 起的那一档。
FREIGHT_ALGORITHM_SOURCE = (
    "https://solutions.shopee.cn/sellers/pricing-simulator/js/app.bb24a38e.js 的 ke() 函数"
)


def _tier_fee(tier: dict, weight_g: float) -> float:
    """单档的贡献。WeightRange 给一次 original_fee；Increment 按已开始的档数乘单价。"""
    start = tier.get("start_weight_g") or 0.0
    if not weight_g > start:
        return 0.0
    if tier.get("mode") == "Flat":
        return float(tier.get("fee") or 0.0)
    if tier.get("mode") == "WeightRange":
        return float(tier.get("fee") or 0.0)
    if tier.get("mode") == "Increment":
        unit = tier.get("increment_unit_g")
        amount = tier.get("increment_amount")
        if not unit or amount is None:
            return 0.0
        end = tier.get("end_weight_g")
        span = (min(weight_g, end) if end else weight_g) - start
        return float(math.ceil(max(span, 0.0) / unit) * amount)
    return 0.0


def tier_fee(tiers: list[dict], weight_g: float, payer: str = "Seller") -> float | None:
    """按模拟器的算法算某个付款方在一组档位下的费用。

    ``tiers`` 用 :func:`rate_cards` 的值（平铺后的字段）。没有对应档位时返回 None——
    不能把"查不到"当成 0，那是这个项目最基本的纪律。
    """
    if weight_g is None or weight_g <= 0:
        return None
    mine = [tier for tier in tiers if (tier.get("payer") or "Seller") == payer]
    if not mine:
        return None
    flat = next((tier for tier in mine if tier.get("mode") == "Flat"), None)
    if flat is not None:
        return float(flat.get("fee") or 0.0)
    return sum(_tier_fee(tier, weight_g) for tier in mine)


def channels_of(config: dict, site: str) -> dict[str, list[dict]]:
    """某站点下 {货类/渠道: 档位列表}，用于按重量取费。"""
    cards = rate_cards(config)
    grouped: dict[str, list[dict]] = {}
    for key, card in cards.items():
        parts = key.split("/")
        if parts[0] != site:
            continue
        grouped.setdefault("%s/%s" % (parts[1], parts[2]), []).append(card)
    return grouped


def seller_fee(config: dict, site: str, channel: str, weight_g: float,
               cargo: str | None = None) -> float | None:
    """按重量算卖家承担的跨境物流成本（藏价）。``channel`` 支持部分匹配。"""
    grouped = channels_of(config, site)
    matches = [name for name in grouped if channel in name and (cargo is None or cargo in name)]
    if not matches:
        return None
    # 命中多个渠道时取最具体的那个（名字最短），避免 "宅配" 匹配到 "宅配（海運）"
    best = min(matches, key=len)
    return tier_fee(grouped[best], weight_g, "Seller")


def buyer_fee(config: dict, site: str, channel: str, weight_g: float = 1.0,
              cargo: str | None = None) -> float | None:
    """买家实付运费。买家侧多为 Flat，与重量无关。"""
    grouped = channels_of(config, site)
    matches = [name for name in grouped if channel in name and (cargo is None or cargo in name)]
    if not matches:
        return None
    return tier_fee(grouped[min(matches, key=len)], weight_g, "Buyer")


def load_latest_config(directory: Path | str = REFERENCE_DIR) -> dict | None:
    """读仓库里最新的那份运费表快照。"""
    path = latest_reference(directory)
    if not path:
        return None
    return json.loads(path.read_text(encoding="utf-8"))["data"]
