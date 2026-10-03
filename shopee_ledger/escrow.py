"""把 v2.payment.get_escrow_detail 的 order_income 映到手册字段。

服务费不自动并进平台费或运费，等卖家中心确认归属后再填 service_fee_kind。
"""

from __future__ import annotations


def order_income(payload: dict) -> dict:
    if "order_income" in payload and isinstance(payload["order_income"], dict):
        return payload["order_income"]
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("order_income"), dict):
        return response["order_income"]
    raise ValueError("JSON 里没有 order_income")


def map_escrow(payload: dict) -> dict:
    income = order_income(payload)
    sls = _num(income, "actual_shipping_fee")
    buyer = _num(income, "buyer_paid_shipping_fee")
    service = income.get("service_fee")
    mapped = {
        "sls_fee": sls,
        "buyer_shipping": buyer,
        "net_shipping": None if sls is None or buyer is None else max(sls - buyer, 0.0),
        "commission": _num(income, "commission_fee"),
        "transaction_fee": _num(income, "transaction_fee"),
        "service_fee": None if service is None else float(service),
        "service_fee_kind": None,
    }
    return mapped


def _num(income: dict, key: str) -> float | None:
    if key not in income or income[key] is None:
        return None
    return float(income[key])
