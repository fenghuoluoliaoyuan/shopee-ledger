"""【遗留 / 参考实现】v1.4 的利润公式。缺费率时返回 incomplete，不用 0 冒充已核实。

⚠️ 现行路径已换成 ``cost_engine.CostEngine``（参数来自 spec、含进口税负承担方、产出证据链）。
本模块现在只剩两处用途：
1. 对外暴露 ``Decision`` 枚举（CLI / 网页仍在用）；
2. 作为公式口径的参考实现，由 ``tests/test_ledger.py::ProfitTest`` 锁住语义。
请勿在业务代码里再调用 ``evaluate``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Decision(str, Enum):
    GO = "go"
    WATCH = "watch"
    CUT = "cut"
    INCOMPLETE = "incomplete"
    THRESHOLD_UNSET = "threshold_unset"


@dataclass
class ProfitInput:
    site: str
    price: float | None
    purchase_cny: float | None
    domestic_cny: float | None
    local_per_cny: float | None
    sls_fee: float | None
    buyer_shipping: float | None
    intends_free_shipping: bool
    prelist: bool
    commission: float | None
    transaction_fee: float | None
    service_fee: float | None
    service_fee_kind: str | None
    affiliate: float
    ads: float
    withdrawal: float | None
    fx_loss: float | None
    return_rate: float | None
    go_rate: float | None
    watch_rate: float | None


@dataclass
class ProfitResult:
    decision: Decision
    net: float | None = None
    rate: float | None = None
    net_shipping: float | None = None
    platform_fee: float | None = None
    return_reserve: float | None = None
    purchase_local: float | None = None
    domestic_local: float | None = None
    missing: list[str] = field(default_factory=list)


def evaluate(data: ProfitInput) -> ProfitResult:
    missing: list[str] = []
    if data.price is None or data.price <= 0:
        missing.append("price")
    if data.purchase_cny is None or data.purchase_cny < 0:
        missing.append("purchase_cny")
    if data.domestic_cny is None or data.domestic_cny < 0:
        missing.append("domestic_cny")
    if data.local_per_cny is None or data.local_per_cny <= 0:
        missing.append("local_per_cny")
    if data.sls_fee is None or data.sls_fee < 0:
        missing.append("sls_fee")
    buyer = data.buyer_shipping
    if data.prelist and data.intends_free_shipping:
        buyer = 0.0
    if buyer is None or buyer < 0:
        missing.append("buyer_shipping")
    if data.commission is None or data.commission < 0:
        missing.append("commission")
    if data.transaction_fee is None or data.transaction_fee < 0:
        missing.append("transaction_fee")
    kind = data.service_fee_kind
    if kind not in ("service", "shipping", "none"):
        missing.append("service_fee_kind")
    elif kind == "service" and (data.service_fee is None or data.service_fee < 0):
        missing.append("service_fee")
    if data.withdrawal is None or data.withdrawal < 0:
        missing.append("withdrawal")
    if data.fx_loss is None or data.fx_loss < 0:
        missing.append("fx_loss")
    if data.return_rate is None or not 0 <= data.return_rate <= 1:
        missing.append("return_rate")
    if data.go_rate is None or data.watch_rate is None:
        return ProfitResult(Decision.THRESHOLD_UNSET, missing=missing or ["go_rate"])
    if data.go_rate < data.watch_rate:
        missing.append("threshold_order")
    if missing:
        return ProfitResult(Decision.INCOMPLETE, missing=missing)

    assert data.price is not None
    assert data.purchase_cny is not None and data.domestic_cny is not None
    assert data.local_per_cny is not None and data.sls_fee is not None
    assert buyer is not None and data.commission is not None
    assert data.transaction_fee is not None and data.withdrawal is not None
    assert data.fx_loss is not None and data.return_rate is not None
    assert data.go_rate is not None and data.watch_rate is not None

    purchase_local = data.purchase_cny * data.local_per_cny
    domestic_local = data.domestic_cny * data.local_per_cny
    net_shipping = max(data.sls_fee - buyer, 0.0)
    service = data.service_fee if kind == "service" else 0.0
    platform = data.commission + data.transaction_fee + (service or 0.0)
    reserve = data.return_rate * (purchase_local + domestic_local)
    net = (
        data.price
        - purchase_local
        - domestic_local
        - net_shipping
        - platform
        - data.affiliate
        - data.ads
        - data.withdrawal
        - data.fx_loss
        - reserve
    )
    rate = net / data.price
    if rate >= data.go_rate:
        decision = Decision.GO
    elif rate >= data.watch_rate:
        decision = Decision.WATCH
    else:
        decision = Decision.CUT
    return ProfitResult(
        decision=decision,
        net=net,
        rate=rate,
        net_shipping=net_shipping,
        platform_fee=platform,
        return_reserve=reserve,
        purchase_local=purchase_local,
        domestic_local=domestic_local,
    )
