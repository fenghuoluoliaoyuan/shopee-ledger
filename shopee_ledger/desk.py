"""今日待办和能否上架。阈值 50% / 30% 按手册标为经验值。"""

from __future__ import annotations

from shopee_ledger.profit import Decision, ProfitResult


def survival_advice(rate: float | None) -> str:
    if rate is None:
        return "还没有录完数据的候选"
    if rate >= 0.5:
        return "经验值：上架可做品，跑第一单"
    if rate >= 0.3:
        return "经验值：砍亏损品，再补选 5 个候选"
    return "经验值：换类目或换价格带"


def listing_gate(
    result: ProfitResult,
    supplier_count: int,
    sample_bought: bool,
    weighed: bool,
    photo_ready: bool,
    title_ready: bool,
    detail_ready: bool,
) -> str:
    if result.missing and result.missing[0].startswith("veto:"):
        return "一票否决，不上架"
    if result.decision == Decision.CUT:
        return "利润不到门槛，不上架"
    if result.decision == Decision.THRESHOLD_UNSET:
        return "台湾站阈值还没设"
    if result.decision == Decision.INCOMPLETE:
        return "先补齐费率再判断"
    if result.decision == Decision.WATCH:
        return "观察，先改售价或补选"
    if not sample_bought:
        return "先买样品再称重"
    if not weighed:
        return "先称重，再查运费档"
    if supplier_count < 3:
        return f"还差 {3 - supplier_count} 家代发供应商"
    if not photo_ready:
        return "主图还没用样品自己拍"
    if not title_ready:
        return "标题还没写"
    if not detail_ready:
        return "详情前 3 屏还没写"
    return "可上架"
