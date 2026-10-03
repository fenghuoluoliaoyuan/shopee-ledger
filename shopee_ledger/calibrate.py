"""KPI 校准：把「经验值」和「实测值」摆在一起，说清该不该改阈值。

为什么需要
----------
12 个 D 级参数是**经验值**，其中 P-TW-MARGIN-TH（≥15% 可做 / ≥10% 观察）直接驱动
R-COST-002「砍掉」——阈值定错了，整个选品方向就是错的。spec 自己也标了风险：
「对无货源铺货可能过严。若存活率 <30%，应先怀疑阈值而非方向」。

架构 v4.0 把 KpiCalibrator 列为平台级能力，但一直没有实现。这里补上。

两条纪律
--------
1. **样本不够就明说不够，不给建议值。** 三个订单算出来的"实际退货率"没有任何意义，
   拿它去改阈值比不改更危险。
2. **只报告，不自动改。** 校准是判断，判断留给人（或显式确认的智能体）。
   与"自动抓取 ≠ 自动生效"是同一条规矩。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable

# 默认低于这个样本数就只报"数据不足"
DEFAULT_MIN_SAMPLES = 5
# 观测值相对声明值偏离超过这个比例才算"漂移"
DEFAULT_TOLERANCE = 0.10

INSUFFICIENT = "insufficient"
CONSISTENT = "consistent"
DRIFT = "drift"

STATUS_LABEL = {INSUFFICIENT: "数据不足", CONSISTENT: "与实测一致", DRIFT: "与实测偏离"}


@dataclass
class CalibrationItem:
    """一个参数的校准结论。"""

    param_id: str
    name: str
    kind: str                       # rate（费率）| threshold（阈值）
    declared: Any
    observed: float | None = None
    samples: int = 0
    needed: int = DEFAULT_MIN_SAMPLES
    status: str = INSUFFICIENT
    deviation: float | None = None  # 相对偏离，正数表示实测高于声明
    needs: str = ""                 # 还缺什么数据才能校准
    note: str = ""

    @property
    def label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)

    @property
    def actionable(self) -> bool:
        """够样本、且确实偏离——只有这种才值得人去看一眼。"""
        return self.status == DRIFT


def _relative(observed: float, declared: float) -> float | None:
    if not declared:
        return None
    return (observed - declared) / declared


def compare_rate(param_id: str, name: str, declared: float, observed_values: Iterable[float],
                 *, min_samples: int = DEFAULT_MIN_SAMPLES,
                 tolerance: float = DEFAULT_TOLERANCE, needs: str = "") -> CalibrationItem:
    """费率类：用账单实付反算的实际费率与声明值比。

    取中位数而不是平均——账单里偶尔会有异常单（退款、调整），平均值会被带偏。
    """
    values = [float(v) for v in observed_values if v is not None]
    item = CalibrationItem(param_id=param_id, name=name, kind="rate",
                           declared=declared, samples=len(values), needed=min_samples,
                           needs=needs or "需要 %d 单带账单实付的已完成订单" % min_samples)
    if len(values) < min_samples:
        return item
    observed = median(values)
    item.observed = observed
    item.deviation = _relative(observed, declared)
    if item.deviation is not None and abs(item.deviation) > tolerance:
        item.status = DRIFT
        item.note = "实测中位数 %s，声明 %s，偏离 %.1f%%（容差 %.0f%%）" % (
            _fmt(observed), _fmt(declared), item.deviation * 100, tolerance * 100)
    else:
        item.status = CONSISTENT
        item.note = "实测中位数 %s 与声明 %s 相符" % (_fmt(observed), _fmt(declared))
    return item


def compare_threshold(param_id: str, name: str, declared: Any, observed_values: Iterable[float],
                      *, min_samples: int = DEFAULT_MIN_SAMPLES,
                      needs: str = "") -> CalibrationItem:
    """阈值类：看实测分布落在阈值哪一侧。

    阈值不像费率那样"对上就完事"——它要回答的是「这个阈值定得合不合适」。
    所以输出的是观测中位数与阈值的相对位置，**不自动给新阈值**：
    样本量小的时候，把阈值往观测值上凑是过拟合。
    """
    values = [float(v) for v in observed_values if v is not None]
    reference = None
    if isinstance(declared, dict):
        reference = declared.get("pass") or declared.get("healthy") or declared.get("min_days")
    elif isinstance(declared, (int, float)) and not isinstance(declared, bool):
        reference = float(declared)
    item = CalibrationItem(param_id=param_id, name=name, kind="threshold",
                           declared=declared, samples=len(values), needed=min_samples,
                           needs=needs or "需要 %d 个样本才能判断阈值是否合适" % min_samples)
    if len(values) < min_samples:
        return item
    observed = median(values)
    item.observed = observed
    if reference:
        item.deviation = _relative(observed, float(reference))
    # 阈值类不判"漂移"，只陈述实测分布与阈值的关系
    item.status = CONSISTENT if (item.deviation is None or abs(item.deviation) <= 0.5) else DRIFT
    if reference is None:
        item.note = "实测中位数 %s（该参数没记可比的单一阈值，只报观测值）" % _fmt(observed)
    else:
        side = "高于" if observed >= float(reference) else "低于"
        item.note = "实测中位数 %s，%s声明阈值 %s——阈值可能%s" % (
            _fmt(observed), side, _fmt(float(reference)),
            "偏严（观测普遍达不到）" if observed < float(reference) else "偏松（实测轻松达标）")
    return item


def _fmt(value: float) -> str:
    if abs(value) >= 1000:
        return "%.0f" % value
    if abs(value) >= 1:
        return "%.2f" % value
    return "%.4f" % value


def summary(items: list[CalibrationItem]) -> dict[str, Any]:
    return {
        "total": len(items),
        "drift": [item.param_id for item in items if item.status == DRIFT],
        "consistent": [item.param_id for item in items if item.status == CONSISTENT],
        "insufficient": [item.param_id for item in items if item.status == INSUFFICIENT],
        "samples": sum(item.samples for item in items),
    }


def render(items: list[CalibrationItem]) -> str:
    stats = summary(items)
    lines = ["KPI 校准：拿实测值对照经验值（%d 个参数，共 %d 个样本）"
             % (stats["total"], stats["samples"]), ""]
    width = max((len(item.name) for item in items), default=10)
    for item in sorted(items, key=lambda i: (i.status == INSUFFICIENT, i.param_id)):
        mark = {DRIFT: "⚠", CONSISTENT: "✓", INSUFFICIENT: "·"}[item.status]
        observed = "—" if item.observed is None else _fmt(item.observed)
        lines.append("%s %-26s %-*s  声明 %-22s 实测 %-10s %2d/%d 样本"
                     % (mark, item.param_id, width, item.name,
                        str(item.declared)[:22], observed, item.samples, item.needed))
        if item.note:
            lines.append("    %s" % item.note)
        if item.status == INSUFFICIENT:
            lines.append("    需要：%s" % item.needs)
    lines.append("")
    lines.append("⚠ 偏离 %d 个 ｜ ✓ 一致 %d 个 ｜ · 数据不足 %d 个"
                 % (len(stats["drift"]), len(stats["consistent"]), len(stats["insufficient"])))
    lines.append("**只报告，不自动改阈值**——校准是判断。样本不足时不给建议值：")
    lines.append("拿三个订单算出来的「实际退货率」去改阈值，比不改更危险。")
    return "\n".join(lines)
