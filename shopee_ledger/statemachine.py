"""spec 驱动的履约状态机：**顺序由 transitions 保证，前置条件由 rules 保证**。

设计要点
--------
* 状态、转移、异常分支全部读自 ``modes/<id>.json``；代码里 **不出现状态名常量**。
* ``fire()`` 只做**结构校验**（该转移是否声明过）。业务前置条件由
  ``GateService.check_rules(transition.rules)`` 执行——本模块不认识规则，也不该认识。
* 非法转移的报错必须列出"当前允许去哪"，这是人工操作时最需要的信息。
* ``modes/*.json`` 里的 ``transitions[].guard`` 与 ``exceptions[].condition`` 是**给人看的说明**；
  实际把关的是 ``rules`` 与下面 ``exceptions()`` 里按同一语义编码的实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# EX-02 的提前预警窗口：距到仓扫描截止不足这些小时就报。
# 不再是"进入某状态满 72 小时"——那条既不看站点也不看下单时刻。
EX02_WARN_HOURS = 24
from typing import Any

from shopee_ledger.spec import Spec, default_spec


class TransitionError(ValueError):
    """非法状态转移。"""


@dataclass
class Transition:
    from_state: str
    to_state: str
    guard: str = ""
    rules: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class OrderMachine:
    spec: Spec
    mode: str = "dropship"
    state: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        definition = self.spec.mode(self.mode)["order_state_machine"]
        self.definition = definition
        self.initial = definition.get("initial", "created")
        self.states = list(definition.get("states") or [])
        self.transitions = [
            Transition(t["from"], t["to"], t.get("guard", ""),
                       list(t.get("rules") or []), t.get("note", ""))
            for t in definition.get("transitions") or []
        ]
        if self.state is None:
            self.state = self.initial
        if self.state not in self.states:
            raise TransitionError("未知状态: %s" % self.state)

    # ---- 查询 -----------------------------------------------------------
    def transition_for(self, to_state: str) -> Transition | None:
        for item in self.transitions:
            if item.from_state == self.state and item.to_state == to_state:
                return item
        return None

    def allowed(self) -> list[str]:
        return [t.to_state for t in self.transitions if t.from_state == self.state]

    def is_terminal(self) -> bool:
        return not self.allowed()

    def main_path(self) -> list[str]:
        """主路径（不含 cancelled），给进度条用。"""
        return [state for state in self.states if state != "cancelled"]

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "state": self.state, "allowed": self.allowed(),
                "history": list(self.history)}

    # ---- 转移 -----------------------------------------------------------
    def fire(self, to_state: str, *, at: str | None = None) -> dict[str, Any]:
        if self.state == to_state:
            raise TransitionError("已经在 %s" % to_state)
        transition = self.transition_for(to_state)
        if transition is None:
            allowed = self.allowed()
            raise TransitionError(
                "不允许从 %s 直接跳到 %s；当前允许：%s"
                % (self.state, to_state, "、".join(allowed) or "（已到终态）"))
        record = {"from": self.state, "to": to_state, "at": at or _now()}
        self.history.append(record)
        self.state = to_state
        return record

    def transition_rules(self, to_state: str) -> list[str]:
        transition = self.transition_for(to_state)
        return list(transition.rules) if transition else []

    # ---- 异常分支 -------------------------------------------------------
    def exceptions(self, *, hours_in_state: float | None = None, dts_ready: bool = False,
                   hours_to_scan_deadline: float | None = None,
                   scan_deadline: str | None = None) -> list[dict]:
        """按 modes/*.json 的 exceptions 语义编码实现。

        spec 里的 condition 是散文（"ship_arranged 后 24h 未 po_created"），
        这里用状态 + 停留时长表达同一件事。阈值仍来自参数（EX-02 依赖 P-TW-DTS 是否为 A/B）。

        EX-02 原先用的是**写死的 72 小时**，而且从"进入该状态的时间"算——两处都不对：
        真正的期限取决于站点与备货时长，而且要**从下单时刻**算起（发货时效从下单就开始走）。
        现在改成对着下单时冻结的到仓扫描截止判。
        """
        alerts: list[dict] = []
        if self.state == "ship_arranged" and hours_in_state is not None and hours_in_state > 24:
            alerts.append({"id": "EX-01", "priority": "P1",
                           "message": "已安排发货但超过 24 小时未拍单：发货时效在走"})
        if self.state == "supplier_shipped" and dts_ready:
            if hours_to_scan_deadline is not None:
                if hours_to_scan_deadline <= 0:
                    alerts.append({"id": "EX-02", "priority": "P1",
                                   "message": "已过到仓扫描截止（%s）：订单可能被取消，"
                                              "立刻查物流" % (scan_deadline or "")})
                elif hours_to_scan_deadline <= EX02_WARN_HOURS:
                    alerts.append({"id": "EX-02", "priority": "P1",
                                   "message": "距到仓扫描截止（%s）不到 %d 小时：抓紧催件"
                                              % (scan_deadline or "", EX02_WARN_HOURS)})
            elif hours_in_state is not None and hours_in_state > 72:
                # 没有截止时间的兜底：老订单（开单时还没有发货时效规则）用旧口径，并说明
                alerts.append({"id": "EX-02", "priority": "P1",
                               "message": "供应商已发货但迟迟未到仓，存在迟发风险"
                                          "（这一单没有算出的到仓截止时间，按旧口径 72 小时判）"})
        if self.state == "cancelled":
            alerts.append({"id": "EX-03", "priority": "P1",
                           "message": "订单已取消：先下架商品，取消按卖家中心当时规则处理"})
        return alerts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def machine_for(spec: Spec | None = None, state: str | None = None,
                mode: str = "dropship") -> OrderMachine:
    return OrderMachine(spec or default_spec(), mode=mode, state=state)
