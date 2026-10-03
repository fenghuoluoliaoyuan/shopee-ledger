"""第 6.2 节履约顺序 —— **适配器**：判定来自 ``spec/modes/dropship.json`` 的状态机。

历史：v1.4 时是 6 个布尔步骤 + 一堆 ``if``。v4.0 起是 13 态状态机（含 cancelled）：
* **顺序**由 ``transitions`` 保证（禁止跳步）；
* **前置条件**由 ``transitions[].rules`` 指定的规则保证。
本模块保留旧函数名，让 CLI 与网页可以平滑迁移，但内部不再持有任何顺序判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shopee_ledger.gates import INCOMPLETE, REJECT, GateService
from shopee_ledger.spec import default_spec
from shopee_ledger.statemachine import OrderMachine, TransitionError

STEP_LABEL = {
    "created": "已建单",
    "stock_checked": "查库存",
    "ship_arranged": "安排发货",
    "address_captured": "抄当单地址",
    "po_created": "1688 拍单",
    "supplier_shipped": "供应商发货",
    "warehouse_scanned": "到仓扫描",
    "in_transit": "跨境运输",
    "delivered": "已签收",
    "completed": "订单完成",
    "payable": "待打款",
    "paid": "已到账",
    "cancelled": "已取消",
}


def _definition() -> dict[str, Any]:
    return default_spec().mode("dropship")["order_state_machine"]


def states() -> tuple[str, ...]:
    return tuple(_definition()["states"])


def main_path() -> tuple[str, ...]:
    return tuple(s for s in states() if s != "cancelled")


# 兼容旧调用方（网页进度条按 STEPS 渲染）
STEPS = main_path()


@dataclass
class Fulfillment:
    machine: OrderMachine = field(default_factory=lambda: OrderMachine(default_spec()))
    supplier_id: int | None = None
    warehouse_address: str = ""
    address_source: str = ""
    block_reason: str = ""
    address_format_ok: bool = False

    # 旧代码读 state.steps / state.status；现在都由状态机推导
    @property
    def state(self) -> str:
        return self.machine.state

    @property
    def steps(self) -> dict[str, bool]:
        path = main_path()
        index = path.index(self.state) if self.state in path else -1
        return {step: position <= index for position, step in enumerate(path)}

    @property
    def status(self) -> str:
        if self.state == "cancelled":
            return "all_oos"
        if self.state in ("completed", "payable", "paid"):
            return "done"
        return "open"

    def next_states(self) -> list[str]:
        return self.machine.allowed()


def order_context(state: "Fulfillment") -> dict[str, Any]:
    """把状态机状态映射成守卫规则认得的字段。必须在转移**之前**取样。

    没有这层映射，R-ORDER-001（order.stock_checked_at == null）会因缺字段返回 UNKNOWN，
    把硬规则卡成 INCOMPLETE——即"所有转移都过不去"。
    """
    scanned = state.state in ("warehouse_scanned", "in_transit", "delivered",
                              "completed", "payable", "paid")
    return {
        "order": {"stock_checked_at": None if state.state == "created" else "state:%s" % state.state},
        "po": {"address_source": state.address_source or None},
        "shipment": {"warehouse_scanned_at": "recorded" if scanned else None},
    }


def advance(state: Fulfillment, to_state: str, *, gates: GateService | None = None,
            context: dict[str, Any] | None = None) -> dict[str, Any]:
    """通用推进：先过该转移声明的守卫规则，再 fire。

    守卫规则来自 ``transitions[].rules``——状态机不认识规则，门禁不认识状态，
    两者在这里串起来。
    """
    rules = state.machine.transition_rules(to_state)
    if rules:
        result = (gates or GateService(state.machine.spec)).check_rules(
            rules, context if context is not None else order_context(state),
            gate_id="G5", mode=state.machine.mode)
        if result.result in (REJECT, INCOMPLETE):
            blocker = (result.fired or result.unknown)
            reason = blocker[0].message if blocker else result.result
            unknown = blocker[0].unknowns if blocker else []
            raise ValueError("守卫未通过（%s）：%s%s"
                             % (result.result, reason,
                                ("；缺字段 " + "、".join(unknown)) if unknown else ""))
    return state.machine.fire(to_state)


# ---- 旧函数名（保留语义，内部改走状态机） --------------------------------
def mark_stock(state: Fulfillment, supplier_id: int, in_stock: bool,
               address_ok: bool, exhausted: bool) -> None:
    if state.state != "created":
        raise ValueError("订单已离开建单状态，不能再查库存")
    if not in_stock:
        if exhausted:
            state.machine.fire("cancelled")
            state.block_reason = "全部供应商无货：先下架商品，订单取消按卖家中心当时规则处理"
        return
    state.supplier_id = supplier_id
    state.machine.fire("stock_checked")
    state.address_format_ok = bool(address_ok)
    state.block_reason = ""


def confirm_address_format(state: Fulfillment) -> None:
    if state.state == "created":
        raise ValueError("先确认有货，再确认地址格式")
    state.address_format_ok = True


def arrange_ship(state: Fulfillment, *, gates: GateService | None = None,
                 context: dict[str, Any] | None = None) -> None:
    if state.state == "cancelled":
        raise ValueError(state.block_reason or "订单已取消")
    advance(state, "ship_arranged", gates=gates, context=context)


def copy_address(state: Fulfillment, address: str, source: str, *,
                 gates: GateService | None = None, context: dict[str, Any] | None = None) -> None:
    if source != "order_page":
        raise ValueError("地址只能来自当单页面，不能用网上的通用仓地址")
    text = address.strip()
    if not text:
        raise ValueError("当单地址为空")
    advance(state, "address_captured", gates=gates, context=context)
    state.warehouse_address = text
    state.address_source = source


def mark_purchased(state: Fulfillment, *, gates: GateService | None = None,
                   context: dict[str, Any] | None = None) -> None:
    advance(state, "po_created", gates=gates, context=context)


def mark_supplier_shipped(state: Fulfillment) -> None:
    state.machine.fire("supplier_shipped")


def mark_warehouse_scanned(state: Fulfillment) -> None:
    state.machine.fire("warehouse_scanned")


def mark_inbound(state: Fulfillment) -> None:
    """交仓 = 供应商发货 + 到仓扫描，两步依次走（保留旧命令语义）。

    必须容忍"已经在 supplier_shipped"：CLI 的 order-inbound 和网页的
    「到仓扫描」都可能从该状态调用，否则会撞出"已经在 supplier_shipped"。
    """
    if state.state == "po_created":
        mark_supplier_shipped(state)
    if state.state == "supplier_shipped":
        mark_warehouse_scanned(state)
