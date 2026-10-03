"""第 6.2 节履约顺序。先查库存，再确认地址格式，然后才安排发货。"""

from __future__ import annotations

from dataclasses import dataclass, field

STEPS = (
    "stock_checked",
    "address_format_ok",
    "ship_arranged",
    "address_copied",
    "purchased",
    "handed_to_warehouse",
)


@dataclass
class Fulfillment:
    status: str = "open"
    supplier_id: int | None = None
    steps: dict[str, bool] = field(default_factory=lambda: {step: False for step in STEPS})
    warehouse_address: str = ""
    address_source: str = ""
    block_reason: str = ""

    def ready(self, step: str) -> bool:
        index = STEPS.index(step)
        return all(self.steps[name] for name in STEPS[:index])


def mark_stock(state: Fulfillment, supplier_id: int, in_stock: bool, address_ok: bool, exhausted: bool) -> None:
    if state.status != "open":
        raise ValueError("订单已结束，不能再查库存")
    if not in_stock:
        if exhausted:
            state.status = "all_oos"
            state.block_reason = "三家都无货：先下架，取消按卖家中心当时规则处理"
        return
    state.supplier_id = supplier_id
    state.steps["stock_checked"] = True
    state.block_reason = ""
    if address_ok:
        state.steps["address_format_ok"] = True


def confirm_address_format(state: Fulfillment) -> None:
    if not state.steps["stock_checked"]:
        raise ValueError("先确认有货，再确认地址格式")
    state.steps["address_format_ok"] = True


def arrange_ship(state: Fulfillment) -> None:
    if state.status == "all_oos":
        raise ValueError(state.block_reason)
    if not state.ready("ship_arranged"):
        raise ValueError("先查库存并确认接受中转仓地址，再安排发货")
    state.steps["ship_arranged"] = True


def copy_address(state: Fulfillment, address: str, source: str) -> None:
    if not state.ready("address_copied"):
        raise ValueError("先在卖家中心安排发货，再抄当单地址")
    if source != "order_page":
        raise ValueError("地址只能来自当单页面，不能用网上的通用仓地址")
    text = address.strip()
    if not text:
        raise ValueError("当单地址为空")
    state.warehouse_address = text
    state.address_source = source
    state.steps["address_copied"] = True


def mark_purchased(state: Fulfillment) -> None:
    if not state.ready("purchased"):
        raise ValueError("先抄当单中转仓地址再拍单")
    state.steps["purchased"] = True


def mark_inbound(state: Fulfillment) -> None:
    if not state.ready("handed_to_warehouse"):
        raise ValueError("先完成 1688 拍单，再记录交仓")
    state.steps["handed_to_warehouse"] = True
    state.status = "done"
