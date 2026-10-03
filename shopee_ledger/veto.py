"""第 3.1 节一票否决 —— **适配器**。

历史：v1.4 时 5 条否决写死在本模块的 ``VETO_FLAGS`` 里。
v4.0 起唯一来源是 ``spec/rules/platform.json``：
    R-SEL-001 品牌 / IP / 卡通
    R-SEL-002 易碎 / 服装鞋帽 / 需认证
    R-SEL-003 禁运词库命中（E 级 → 按 on_degrade 降级为软提示 + 人工复核）
本模块只负责"把旧 flag 翻译成门禁上下文"，不再持有判定逻辑。

语义变化（有意为之）：液体 / 粉末 / 电池 在 v1.4 是硬否决，v4.0 归入禁运词库，
因词库本身是 E 级待核实，只能给出软提示，不许硬拦——见 INV-001 / INV-011。
"""

from __future__ import annotations

from shopee_ledger.gates import REJECT, WARN, GateService, INCOMPLETE
from shopee_ledger.spec import Spec, default_spec

FLAG_CONTEXT = {
    "brand_ip": {"has_brand_ip": True},
    "fragile": {"category": "fragile"},
    "apparel": {"category": "apparel"},
    "needs_cert": {"category": "certified_goods"},
}

RESTRICTED_FLAG = "liquid_powder_battery"
RESTRICTED_TERMS = ["液体", "粉末", "电池", "磁铁", "油漆"]

# 供展示：flag → 中文名（不再参与判定）
VETO_FLAGS = {
    "brand_ip": "含品牌 / IP / 卡通形象",
    "liquid_powder_battery": "液体、粉末、电池",
    "fragile": "易碎品",
    "apparel": "服装、鞋帽",
    "needs_cert": "需认证品类",
}


def _context(flags: list[str]) -> dict:
    candidate: dict = {}
    for flag in flags:
        candidate.update(FLAG_CONTEXT.get(flag, {}))
    context: dict = {"candidate": candidate}
    if RESTRICTED_FLAG in flags:
        candidate["title"] = "液体"
        candidate["desc"] = "粉末 电池"
        context["banned_terms"] = RESTRICTED_TERMS
    return context


def evaluate_flags(flags: list[str], spec: Spec | None = None) -> dict:
    """返回 {'blocking': [...], 'advisories': [...], 'result': ...}。"""
    unknown = [f for f in flags if f and f not in VETO_FLAGS]
    if unknown:
        raise ValueError("未知否决项: " + ",".join(unknown))

    spec = spec or default_spec()
    gate = GateService(spec).check("G1", _context(flags), mode="dropship")
    blocking: list[str] = []
    advisories: list[str] = []
    for outcome in gate.fired:
        if outcome.result in (REJECT,):
            blocking.append(outcome.message or outcome.name)
        elif outcome.result == WARN:
            advisories.append(outcome.message or outcome.name)
    return {
        "blocking": blocking,
        "advisories": advisories,
        "result": gate.result,
        "gate": gate,
    }


def veto_reasons(flags: list[str], spec: Spec | None = None) -> list[str]:
    """兼容旧签名：只返回**硬否决**原因。软提示见 ``veto_advisories``。"""
    return evaluate_flags(flags, spec)["blocking"]


def veto_advisories(flags: list[str], spec: Spec | None = None) -> list[str]:
    return evaluate_flags(flags, spec)["advisories"]
