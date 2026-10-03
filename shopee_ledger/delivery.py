"""发货时效：最迟发货时间（DTS）与最迟到仓扫描时间。

规则从哪来
----------
不是从官方措辞读出来的，而是**从接口样本归纳、再样本外验证 630/630** 得到的。
官方页面写的是「最迟到仓扫描时间 = DTS + 3个自然日（泰国、巴西和阿根廷为 DTS + 3个发货日）」，
但照着这句话写会错两处，实测两个失败样本逼出了真规则：

1. 「发货日」不是跳周六周日，而是**非周日、非节假日——周六算工作日**。
2. 自然日站点的扫描截止**只看日历**，不因节假日顺延；只有发货日站点才走工作日历。

归纳过程见提交记录：第 1 轮 133/160、第 2 轮 342/360、第 3 轮 630/630。
"按措辞猜算法"是这个项目反复吃亏的地方，所以这里把验证固化成 `verify_samples()`，
规则若被人改错，对样本一跑就露馅。

数据来源
--------
``GET  /sellers/delivery-calculator/api/get-exempt-days``   豁免日期表（40 条）
``POST /sellers/delivery-calculator/api/get-result``        {site, order_date, dts}
两者都不需要登录，成功码 200000。原始返回在 ``spec/reference/delivery-*.json``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REFERENCE = ROOT / "spec" / "reference"

# 扫描截止按「发货日」而不是自然日推进的站点（官方明确点名泰/巴/阿）
BUSINESS_DAY_MARKETS = frozenset({"TH", "BR", "AR"})

# 发货时间计算器覆盖的跨境站点（9 个）。
# 比项目当前的 5 个市场维度更宽——**数据是官方给全的，没必要因为维度还没扩就不让算**。
# 这个集合只用来拦住拼错的站点代码，不是"项目支持的市场"列表。
CALCULATOR_MARKETS = frozenset({"TW", "MY", "PH", "SG", "TH", "VN", "BR", "MX", "AR"})
# 备货时长默认 1 天——官方计算器页面默认值也是 1
DEFAULT_DTS_DAYS = 1
SCAN_GRACE_DAYS = 3

SOURCE_URL = ("https://solutions.shopee.cn/sellers/delivery-calculator/#/order-date")
EXEMPT_API = "https://solutions.shopee.cn/sellers/delivery-calculator/api/get-exempt-days"
RESULT_API = "https://solutions.shopee.cn/sellers/delivery-calculator/api/get-result"


@dataclass
class DeliveryDeadline:
    market: str
    order_day: date
    dts_days: int
    dts_day: date
    scan_day: date
    holidays_hit: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def scan_rule(self) -> str:
        return "DTS + 3 发货日" if self.market in BUSINESS_DAY_MARKETS else "DTS + 3 自然日"

    def render(self) -> str:
        lines = [
            "%s 站：下单 %s，备货 %d 天" % (self.market, self.order_day.isoformat(), self.dts_days),
            "  最迟发货时间（DTS）    %s 23:59" % self.dts_day.isoformat(),
            "  最迟到仓扫描时间       %s 23:59   （%s）" % (self.scan_day.isoformat(),
                                                          self.scan_rule),
        ]
        if self.holidays_hit:
            lines.append("  其间顺延的节假日       %s" % "、".join(self.holidays_hit))
        if self.note:
            lines.append("  %s" % self.note)
        return "\n".join(lines)


def load_holidays(reference: Path | str | None = None) -> dict[date, str]:
    """豁免日期表：日期 → 名称。所有站点共用同一份（接口的 region 覆盖全部 9 站）。

    取自 ``spec/reference/delivery-exempt-days.json``。文件不在就返回空表，
    此时只按「非周日」推进——**不假装知道节假日**，并在结论里说明。
    """
    path = Path(reference or DEFAULT_REFERENCE) / "delivery-exempt-days.json"
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    holidays: dict[date, str] = {}
    for item in doc.get("data") or []:
        start = date.fromisoformat(item["date_start"])
        end = date.fromisoformat(item["date_end"])
        current = start
        while current <= end:
            holidays[current] = item.get("date_name") or "节假日"
            current += timedelta(days=1)
    return holidays


def is_shipping_day(day: date, holidays: dict[date, str] | None = None) -> bool:
    """是不是一个「发货日」。

    **周六算工作日**——这一点是从样本反推出来的：TW 10-16（周五）下单备货 1 天，
    DTS 落在 10-17 周六而不是顺延到周一；而 10-17 周六下单则顺延到 10-19 周一。
    所以只排除周日与节假日。
    """
    if day.weekday() == 6:
        return False
    return day not in (holidays or {})


def add_shipping_days(start: date, count: int,
                      holidays: dict[date, str] | None = None) -> date:
    """从 start 往后推 count 个发货日。count 为 0 时返回不早于 start 的发货日。"""
    current, added = start, 0
    while added < count:
        current += timedelta(days=1)
        if not is_shipping_day(current, holidays):
            continue
        added += 1
    while not is_shipping_day(current, holidays):
        current += timedelta(days=1)
    return current


def add_scan_days(start: date, market: str,
                  holidays: dict[date, str] | None = None) -> date:
    """从 DTS 往后推到仓扫描截止。"""
    if market.upper() in BUSINESS_DAY_MARKETS:
        return add_shipping_days(start, SCAN_GRACE_DAYS, holidays)
    # 自然日站点：**纯日历天数**，不因节假日顺延（实测 TW 09-29 + 3 = 10-02，
    # 中间夹着国庆 10-01 也照样是 10-02）
    return start + timedelta(days=SCAN_GRACE_DAYS)


def holidays_between(start: date, end: date,
                     holidays: dict[date, str] | None = None) -> list[str]:
    table = holidays or {}
    names: list[str] = []
    current = start
    while current <= end:
        name = table.get(current)
        if name and name not in names:
            names.append(name)
        current += timedelta(days=1)
    return names


# 中国不实行夏令时，固定 UTC+8 即可。用 timezone 而不是 zoneinfo：
# zoneinfo 在 Windows 上要靠 tzdata，而 zoneinfo 是标准库、tzdata 不是——
# 这个项目坚持零新依赖，所以不引入那个不确定性。
CHINA_TZ = timezone(timedelta(hours=8), name="CST")


def order_day_of(created_at: str | datetime) -> date:
    """把订单创建时间（UTC）换成一个「中国时区的日期」。

    发货时效是按北京时间算的：官方计算器的输入是 `%Y-%m-%d %H:%M:%S`，
    返回的时间戳也在该时区。订单表里存的是 UTC，所以要换过来再取日期。
    """
    if isinstance(created_at, datetime):
        moment = created_at
    else:
        moment = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CHINA_TZ).date()


def deadlines_at(created_at: str | datetime, *, market: str = "TW",
                 dts_days: int = DEFAULT_DTS_DAYS,
                 holidays: dict[date, str] | None = None,
                 reference: Path | str | None = None,
                 known_markets: set[str] | None = None) -> DeliveryDeadline:
    """按订单创建时间算截止。"""
    return deadlines(order_day_of(created_at), market=market, dts_days=dts_days,
                     holidays=holidays, reference=reference, known_markets=known_markets)


def end_of_day_iso(day: date) -> str:
    """当天 23:59（中国时区）的 ISO 时刻，**带偏移量**。

    不带偏移量的话，和 UTC 的 now() 一比就差 8 小时——「还有 3 小时」会算成「还有 11 小时」。
    """
    return "%sT23:59:00+08:00" % day.isoformat()


def hours_until(moment_iso: str, now: datetime | None = None) -> float | None:
    """距离某个「当天 23:59」的截止还有多少小时。负数表示已过期。"""
    if not moment_iso:
        return None
    try:
        target = datetime.fromisoformat(str(moment_iso))
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (target - current).total_seconds() / 3600.0


def deadlines(order_day: date, *, market: str = "TW", dts_days: int = DEFAULT_DTS_DAYS,
              holidays: dict[date, str] | None = None,
              reference: Path | str | None = None,
              known_markets: set[str] | None = None) -> DeliveryDeadline:
    """算出一个订单的最迟发货时间与最迟到仓扫描时间。

    ``known_markets`` 默认取计算器覆盖的 9 个站点。**必须校验**：只有 TH/BR/AR 走
    「发货日」口径，其他都走自然日——站点拼错的话会**静默套用另一种规则**，
    算出一个看起来正常其实错的日期。实测 `delivery --market ZZ` 就真的算出了结果。
    """
    code = market.upper()
    allowed = CALCULATOR_MARKETS if known_markets is None else known_markets
    if code not in allowed:
        raise ValueError("未知站点 %r；计算器覆盖：%s"
                         % (market, "、".join(sorted(allowed))))
    table = load_holidays(reference) if holidays is None else holidays
    dts_day = add_shipping_days(order_day, max(0, dts_days), table)
    scan_day = add_scan_days(dts_day, code, table)
    note = ""
    if not table:
        note = "没有豁免日期表，只按非周日推进——节假日顺延可能不准"
    return DeliveryDeadline(market=code, order_day=order_day, dts_days=dts_days,
                            dts_day=dts_day, scan_day=scan_day,
                            holidays_hit=holidays_between(order_day, scan_day, table),
                            note=note)


def verify_samples(reference: Path | str | None = None) -> dict[str, object]:
    """拿存下来的口径样本回验规则。规则被改错时这里会立刻报出来。"""
    folder = Path(reference or DEFAULT_REFERENCE)
    path = folder / "delivery-deadline-samples.json"
    if not path.exists():
        return {"available": False, "checked": 0, "mismatched": [], "note": "没有样本文件"}
    doc = json.loads(path.read_text(encoding="utf-8"))
    table = load_holidays(folder)
    bad: list[dict[str, str]] = []
    checked = 0
    for item in doc.get("samples") or []:
        checked += 1
        predicted = deadlines(date.fromisoformat(item["order_date"]),
                              market=item["site"], dts_days=int(item["dts"]),
                              holidays=table)
        if (predicted.dts_day.isoformat() != item["latest_delivery_time"][:10]
                or predicted.scan_day.isoformat() != item["latest_scan_time"][:10]):
            bad.append({"site": item["site"], "order_date": item["order_date"],
                        "dts": str(item["dts"]),
                        "expected": "%s/%s" % (item["latest_delivery_time"][:10],
                                               item["latest_scan_time"][:10]),
                        "predicted": "%s/%s" % (predicted.dts_day.isoformat(),
                                                predicted.scan_day.isoformat())})
    return {"available": True, "checked": checked, "mismatched": bad,
            "source": doc.get("source"), "note": doc.get("note")}
