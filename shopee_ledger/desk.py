"""今日待办与上架门禁 —— **编排器**。

历史：v1.4 时本模块是 13 连 ``if`` 直接返回中文字符串。
v4.0 起判定全部来自 ``spec/rules/*.json``，本模块只做三件事：
    拼上下文 → 跑 GateService → 选一条最该看的文案。
阈值（0.5 / 0.3、供应商 3 家）也不再写死，全部来自 ParamRegistry。
"""

from __future__ import annotations

from typing import Any

from shopee_ledger.gates import INCOMPLETE, REJECT, WARN, GateResult, GateService
from shopee_ledger.spec import Spec, SpecError, default_spec

# 上架要依次过的门：否决 → 实测 → 核算 → 上架质量
LISTING_GATES = ("G1", "G2", "G3", "G4")


def build_context(
    *,
    supplier_count: int = 0,
    sample_bought: bool = False,
    weighed: bool = False,
    purchase_price_cny: float | None = None,
    photo_ready: bool = False,
    title_ready: bool = False,
    detail_ready: bool = False,
    candidate: dict[str, Any] | None = None,
    cost: Any = None,
    market: str = "TW",
    spec: Spec | None = None,
) -> dict[str, Any]:
    spec = spec or default_spec()
    try:
        market_doc: Any = spec.market(market)
    except SpecError:
        market_doc = {}
    # 候选品的否决位是**已知布尔**（旧模型里"未打该 flag"就等于 False），不是未知值。
    # 若留空，R-SEL-001/002 会因缺 candidate 而返回 UNKNOWN，把上架判定卡成 INCOMPLETE。
    context: dict[str, Any] = {
        "candidate": candidate if candidate is not None else {"has_brand_ip": False, "category": ""},
        "suppliers": [{}] * max(0, supplier_count),
        "sample": {"bought": sample_bought},
        "measurement": {"weight_g": 1.0 if weighed else None},
        # 物流规则（R-LOG-002/003/004）用的是裸 weight_g，与 R-DATA-001 的 measurement.weight_g 同源
        "weight_g": 1.0 if weighed else None,
        # 采购实付显式给 None 表示"还没录"，让 R-DATA-002 报缺数据（而不是静默当成 0）
        "purchase_price_cny": purchase_price_cny,
        "listing": {
            "main_image_source": "self_shot" if photo_ready else "",
            "title_ready": title_ready,
            "detail_ready": detail_ready,
        },
        "market": market_doc,
    }
    if cost is not None:
        # 兼容旧的 ProfitResult（无 gate_context，但有 rate/net）
        if hasattr(cost, "gate_context"):
            context.update(cost.gate_context())
        else:
            rate = getattr(cost, "rate", None)
            if rate is not None:
                context["net_margin"] = rate
                context["net_profit"] = getattr(cost, "net", None)
        context["market"] = market_doc
    return context


def listing_gate_result(
    cost: Any = None,
    *,
    market: str = "TW",
    spec: Spec | None = None,
    gates: GateService | None = None,
    **ctx: Any,
) -> list[GateResult]:
    """跑完上架链路上的门禁。G3 只在给了核算结果时才跑（没有经济学结果就无从判定）。"""
    spec = spec or default_spec()
    gates = gates or GateService(spec)
    context = build_context(market=market, spec=spec, cost=cost, **ctx)
    results = []
    for gate_id in LISTING_GATES:
        if gate_id == "G3" and cost is None:
            continue
        results.append(gates.check(gate_id, context, platform="shopee", market=market, mode="dropship"))
    return results


def _render(outcome: Any) -> str:
    text = outcome.message or outcome.name
    if outcome.unknowns:
        text = "%s（缺：%s）" % (text, "、".join(outcome.unknowns))
    return text


def listing_gate(
    cost: Any = None,
    *,
    market: str = "TW",
    spec: Spec | None = None,
    **ctx: Any,
) -> str:
    """返回**阻断原因**的一句话；都不阻断时返回「可上架」。软提示见 ``listing_warnings``。"""
    for gate in listing_gate_result(cost, market=market, spec=spec, **ctx):
        for outcome in gate.outcomes:
            if outcome.result in (REJECT, INCOMPLETE):
                return _render(outcome)
    return "可上架"


def listing_warnings(
    cost: Any = None,
    *,
    market: str = "TW",
    spec: Spec | None = None,
    **ctx: Any,
) -> list[str]:
    """不阻断但该知道的提示（缺数据或经验风险），按门禁顺序去重。"""
    notes: list[str] = []
    for gate in listing_gate_result(cost, market=market, spec=spec, **ctx):
        for outcome in gate.outcomes:
            if outcome.result != WARN:
                continue
            text = _render(outcome)
            if text not in notes:
                notes.append(text)
    return notes


def survival_advice(rate: float | None, spec: Spec | None = None) -> str:
    """存活率建议。阈值来自 P-KPI-SURVIVAL-TH，不在代码里写死。"""
    if rate is None:
        return "还没有录完数据的候选"
    spec = spec or default_spec()
    param = spec.params.get("P-KPI-SURVIVAL-TH")
    thresholds = param.value if param and isinstance(param.value, dict) else {"healthy": 0.5, "weak": 0.3}
    healthy = thresholds.get("healthy", 0.5)
    weak = thresholds.get("weak", 0.3)
    if rate >= healthy:
        return "经验值：上架可做品，跑第一单"
    if rate >= weak:
        return "经验值：砍亏损品，再补选 5 个候选"
    return "经验值：换类目或换价格带"
