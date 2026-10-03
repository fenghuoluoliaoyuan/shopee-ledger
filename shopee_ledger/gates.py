"""GateService：把 ``rules/*.json`` 变成结构化门禁结果。

契约（docs/CONFIG-README.md §3.3）
---------------------------------
每次判定返回：``{gate_id, rule_id, result, evidence_level, message, next_action, param_ids}``
``result ∈ {PASS, INCOMPLETE, WARN, REJECT}``，**禁止只返回布尔值或文案**。

四条铁律的落点
--------------
* INV-001  硬门禁依赖的参数必须是 A/B/C，否则按 ``on_degrade`` 降级；没声明就是配置错误。
* INV-005  仅依赖 E 级参数的规则不得输出 REJECT。
* INV-010  ``stale``（超期未复核）参数不得参与硬判定。
* INV-011  读取 ``unverified_claim`` 的规则必须是 advisory，且只能输出 WARN / INCOMPLETE。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from shopee_ledger.expr import UNKNOWN, ExpressionError, evaluate
from shopee_ledger.spec import Param, Rule, Spec

PASS = "PASS"
INCOMPLETE = "INCOMPLETE"
WARN = "WARN"
REJECT = "REJECT"

# 严重度排序：缺数据(INCOMPLETE) 高于 有风险(WARN)——无法判定时不能装作"确认后即可继续"
SEVERITY = {PASS: 0, WARN: 1, INCOMPLETE: 2, REJECT: 3}

# 配置期规则（由 spec/validate.py 负责），运行期不参与业务判定
CONFIG_ONLY_GATES = ("GOV",)

# 上下文词表：规则 condition 里允许出现的裸名字。测试会强制检查。
# 新增上下文对象时必须同步这里，否则条件会静默变成 UNKNOWN。
CONTEXT_KEYS = frozenset({
    # 对象
    "candidate", "competitor", "listing", "measurement", "sample", "supplier",
    "order", "po", "shipment", "stock_check", "cost_snapshot", "inputs",
    "market", "snapshot", "risk", "import_tax_treatment",
    # 标量
    "weight_g", "dims", "is_liquid", "liquid_ml",
    "sls_freight", "buyer_paid_freight", "net_freight",
    "price_local", "purchase_price_cny", "order_value_local",
    "in_free_window", "commission", "txn_fee", "withdraw_rate",
    "net_margin", "available_cash", "forecast_purchase_7d",
    "in_transit_purchase", "capital", "days_since_last_listing",
    "now", "calibration_window", "lead_time",
    # 词库
    "banned_terms", "brand_terms",
})

NEXT_ACTION = {
    "BLOCK": "修完再继续",
    "WARN": "确认后继续，并留痕",
    "AUTO_FILL": "按经验值填入默认值",
    "TASK": "生成待办/核实任务",
    "ALERT": "发出告警",
}


@dataclass
class RuleOutcome:
    rule_id: str
    name: str
    result: str
    fired: bool | None
    level: str
    action: str
    message: str
    next_action: str
    evidence_level: str | None = None
    param_ids: list[str] = field(default_factory=list)
    degraded: str | None = None
    unknowns: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "result": self.result,
            "fired": self.fired,
            "level": self.level,
            "action": self.action,
            "message": self.message,
            "next_action": self.next_action,
            "evidence_level": self.evidence_level,
            "param_ids": list(self.param_ids),
            "degraded": self.degraded,
            "unknowns": list(self.unknowns),
        }


@dataclass
class GateResult:
    gate_id: str
    result: str
    outcomes: list[RuleOutcome]

    @property
    def fired(self) -> list[RuleOutcome]:
        return [item for item in self.outcomes if item.fired is True]

    @property
    def unknown(self) -> list[RuleOutcome]:
        return [item for item in self.outcomes if item.fired is None]

    @property
    def unknown_fields(self) -> list[str]:
        seen: list[str] = []
        for item in self.outcomes:
            for name in item.unknowns:
                if name not in seen:
                    seen.append(name)
        return seen

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "result": self.result,
            "missing_fields": self.unknown_fields,
            "outcomes": [item.as_dict() for item in self.outcomes],
        }

    def explain(self) -> str:
        lines = ["%s -> %s" % (self.gate_id, self.result)]
        for item in self.outcomes:
            if item.fired is False:
                continue
            mark = {"REJECT": "✗", "WARN": "!", "INCOMPLETE": "?", "PASS": "·"}.get(item.result, " ")
            lines.append("  %s [%s] %s  %s" % (mark, item.level, item.rule_id, item.message or item.name))
            if item.degraded:
                lines.append("      ↳ %s" % item.degraded)
            if item.unknowns:
                lines.append("      ↳ 缺字段: %s" % ", ".join(item.unknowns))
        if self.unknown_fields:
            lines.append("  缺字段汇总: %s" % ", ".join(self.unknown_fields))
        return "\n".join(lines)


class GateService:
    def __init__(self, spec: Spec, today: str | None = None):
        self.spec = spec
        self.today = today or date.today().isoformat()

    # ---- 等级判定 -------------------------------------------------------
    def effective_level(self, rule: Rule) -> tuple[str, str | None]:
        """返回 (生效等级, 降级原因)。生效等级只可能是 hard / soft / advisory。"""
        if rule.level != "hard":
            return rule.level, None

        blockers: list[str] = []
        for pid in rule.depends_on:
            param = self.spec.params.get(pid)
            if param is None:
                blockers.append("%s(缺失)" % pid)
            elif not param.hard_eligible(self.today):
                blockers.append("%s(%s/%s)" % (pid, param.evidence_level, param.effective_state(self.today)))

        if rule.reads_unverified_claim:
            blockers.append("读取 unverified_claim")

        if not blockers:
            return "hard", None
        if rule.on_degrade:
            return "soft", "降级：%s（已声明 on_degrade）" % "; ".join(blockers)
        raise ExpressionError(
            "INV-001 违反：硬规则 %s 依赖 %s 且未声明 on_degrade" % (rule.id, "; ".join(blockers))
        )

    # ---- 主入口 ---------------------------------------------------------
    def check(
        self,
        gate_id: str,
        context: dict[str, Any],
        *,
        platform: str = "*",
        market: str = "*",
        mode: str | None = None,
    ) -> GateResult:
        ctx = dict(context)
        if "params" not in ctx:
            ctx["params"] = self.spec.payload(
                platform=platform, market=market, mode=mode or "*", today=self.today
            )
        outcomes: list[RuleOutcome] = []
        for rule in self.spec.rules_for_gate(gate_id, mode=mode):
            if rule.gate in CONFIG_ONLY_GATES or rule.raw.get("evaluated_by") == "validate.py":
                continue
            outcomes.append(self._evaluate(rule, ctx))
        result = PASS
        for item in outcomes:
            if SEVERITY[item.result] > SEVERITY[result]:
                result = item.result
        return GateResult(gate_id, result, outcomes)

    def _evaluate(self, rule: Rule, context: dict[str, Any]) -> RuleOutcome:
        level, degraded = self.effective_level(rule)
        evidence_level = rule.evidence_level
        param_ids = list(rule.depends_on)
        next_action = rule.raw.get("next_action") or NEXT_ACTION.get(rule.action, "继续")

        try:
            outcome = evaluate(rule.condition, context)
        except ExpressionError as exc:
            return RuleOutcome(
                rule_id=rule.id, name=rule.name, result=INCOMPLETE, fired=None, level=level,
                action=rule.action, message="条件无法求值：%s" % exc, next_action="修配置",
                evidence_level=evidence_level, param_ids=param_ids, degraded=degraded,
                unknowns=[str(exc)],
            )

        if outcome.is_unknown:
            # 硬规则缺数据 → INCOMPLETE（拦住流程）；软/建议规则缺数据 → WARN（提示即可）
            result = INCOMPLETE if level == "hard" else WARN
            return RuleOutcome(
                rule_id=rule.id, name=rule.name, result=result, fired=None, level=level,
                action=rule.action, message=rule.message, next_action="补齐下列字段",
                evidence_level=evidence_level, param_ids=param_ids, degraded=degraded,
                unknowns=list(outcome.unknowns),
            )

        if not outcome.value:
            return RuleOutcome(
                rule_id=rule.id, name=rule.name, result=PASS, fired=False, level=level,
                action=rule.action, message=rule.message, next_action="",
                evidence_level=evidence_level, param_ids=param_ids, degraded=degraded,
            )

        result = self._result_for(rule, level)
        return RuleOutcome(
            rule_id=rule.id, name=rule.name, result=result, fired=True, level=level,
            action=rule.action, message=rule.message, next_action=next_action,
            evidence_level=evidence_level, param_ids=param_ids, degraded=degraded,
            unknowns=list(outcome.unknowns),
        )

    @staticmethod
    def _result_for(rule: Rule, level: str) -> str:
        if rule.result in (PASS, INCOMPLETE, WARN, REJECT):
            base = rule.result
        elif rule.action == "BLOCK":
            base = REJECT
        elif rule.action in ("WARN", "AUTO_FILL"):
            base = WARN
        else:  # TASK / ALERT
            base = WARN
        # 降级后不得 REJECT（INV-001 / INV-005 / INV-011 的收敛点）
        if level != "hard" and base == REJECT:
            return WARN
        return base
